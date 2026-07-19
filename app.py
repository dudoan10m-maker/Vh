from flask import Flask, request, jsonify
from flask_cors import CORS
import os, time, re, json
import psycopg2
import psycopg2.extras

app = Flask(__name__)
CORS(app)  # Cho phép frontend gọi từ mọi domain (Netlify, localhost, v.v.)

# ═══════════════════════════════════════════════════════════════
# LƯU DỮ LIỆU VÀO POSTGRES — bền vững thật sự, KHÔNG mất khi
# service Render deploy lại / ngủ rồi thức dậy (khác với file JSON
# trước đây, chỉ sống trong ổ đĩa tạm của lần chạy đó).
#
# CẦN LÀM TRÊN RENDER (1 lần duy nhất):
#   1. Vào Render Dashboard → New → PostgreSQL (free tier)
#   2. Tạo xong, Render cho 1 "Internal Database URL"
#   3. Vào Web Service (service chạy app.py này) → Environment
#      → thêm biến DATABASE_URL = <Internal Database URL vừa copy>
#   4. Deploy lại — app sẽ tự tạo bảng ở lần chạy đầu tiên.
#
# Nếu CHƯA gắn DATABASE_URL, app vẫn chạy được bằng file JSON tạm
# (data.json) như trước — chỉ để test local, KHÔNG bền vững trên
# Render free tier.
# ═══════════════════════════════════════════════════════════════
DATABASE_URL = os.environ.get('DATABASE_URL', '')

USE_DB = bool(DATABASE_URL)
DB_ERROR = None  # ghi lại lỗi kết nối DB nếu có, để /api/status báo cho biết

if USE_DB:
    # Render đôi khi cấp URL dạng "postgres://" — psycopg2 cần "postgresql://"
    if DATABASE_URL.startswith('postgres://'):
        DATABASE_URL = DATABASE_URL.replace('postgres://', 'postgresql://', 1)

    def get_conn():
        return psycopg2.connect(DATABASE_URL)

    def init_db():
        conn = get_conn()
        cur = conn.cursor()
        cur.execute('''
            CREATE TABLE IF NOT EXISTS accounts (
                id TEXT PRIMARY KEY,
                name TEXT,
                password TEXT,
                email TEXT,
                created_at BIGINT,
                assigned_key TEXT,
                assigned_at BIGINT,
                key_expiry BIGINT,
                device TEXT,
                ip TEXT
            )
        ''')
        cur.execute('''
            CREATE TABLE IF NOT EXISTS keys_store (
                key TEXT PRIMARY KEY,
                exp BIGINT,
                user_name TEXT,
                created BIGINT,
                account_id TEXT,
                max_devices INT DEFAULT 1,
                devices JSONB DEFAULT '[]'::jsonb
            )
        ''')
        cur.execute('''
            CREATE TABLE IF NOT EXISTS settings (
                id INT PRIMARY KEY DEFAULT 1,
                locked BOOLEAN DEFAULT FALSE,
                message TEXT DEFAULT '',
                base_price BIGINT DEFAULT 33000,
                sale_price BIGINT DEFAULT 0,
                sale_expiry BIGINT
            )
        ''')
        # Nếu bảng settings đã tồn tại từ trước (bản cũ chưa có cột giá) -> thêm cột
        cur.execute("ALTER TABLE settings ADD COLUMN IF NOT EXISTS base_price BIGINT DEFAULT 33000")
        cur.execute("ALTER TABLE settings ADD COLUMN IF NOT EXISTS sale_price BIGINT DEFAULT 0")
        cur.execute("ALTER TABLE settings ADD COLUMN IF NOT EXISTS sale_expiry BIGINT")
        cur.execute('INSERT INTO settings (id, locked, message) VALUES (1, FALSE, %s) ON CONFLICT (id) DO NOTHING', ('',))
        # Bảng key-value tổng quát (game-config, v.v.)
        cur.execute('''
            CREATE TABLE IF NOT EXISTS kv_store (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        ''')
        conn.commit()
        cur.close()
        conn.close()

    try:
        init_db()
    except Exception as e:
        # QUAN TRỌNG: nếu kết nối Postgres lỗi (sai URL, DB chưa sẵn sàng...),
        # KHÔNG được âm thầm rơi về JSON tạm — phải báo rõ qua /api/status
        # để không ai tưởng nhầm là đang lưu bền vững trong khi thực ra không.
        DB_ERROR = str(e)
        print('[LỖI KẾT NỐI POSTGRES]', DB_ERROR)

    def _acc_row_to_dict(r):
        return {
            'id': r['id'], 'name': r['name'], 'password': r['password'], 'email': r['email'],
            'createdAt': r['created_at'], 'assignedKey': r['assigned_key'], 'assignedAt': r['assigned_at'],
            'keyExpiry': r['key_expiry'], 'device': r['device'], 'ip': r['ip']
        }

else:
    # ── Chế độ dự phòng: file JSON tạm (chỉ dùng khi chưa gắn DATABASE_URL) ──
    DATA_FILE = 'data.json'

    def _load():
        if os.path.exists(DATA_FILE):
            try:
                with open(DATA_FILE, 'r', encoding='utf-8') as f:
                    d = json.load(f)
                    d.setdefault('accounts', [])
                    d.setdefault('keys', {})
                    d.setdefault('maintenance', {'locked': False, 'message': ''})
                    return d
            except Exception:
                pass
        return {'accounts': [], 'keys': {}, 'maintenance': {'locked': False, 'message': ''}}

    def _save(data):
        try:
            with open(DATA_FILE, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print('[WARN] Không ghi được data.json:', e)

    JSONDB = _load()


def _get_client_ip():
    fwd = request.headers.get('X-Forwarded-For', '')
    if fwd:
        return fwd.split(',')[0].strip()
    return request.remote_addr or 'unknown'


# ─────────────────────────────────────────────────────────────
# 1. POST /register — Đăng ký tài khoản
# Chặn trùng: cùng tên + cùng IP, hoặc trùng email
# ─────────────────────────────────────────────────────────────
@app.route('/register', methods=['POST'])
def register():
    data = request.json or {}
    acc_id = data.get('id')
    name = (data.get('name') or '').strip()
    email = (data.get('email') or '').strip().lower()
    password = data.get('password')
    device = data.get('device')
    client_ip = _get_client_ip()

    if not acc_id or not name:
        return jsonify({'error': 'Thiếu id hoặc name'}), 400

    if USE_DB:
        conn = get_conn()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        cur.execute('SELECT * FROM accounts WHERE id=%s', (acc_id,))
        existing = cur.fetchone()
        if existing:
            cur.execute('''UPDATE accounts SET name=%s, email=%s, password=%s, device=%s, ip=%s WHERE id=%s''',
                        (name, email, password, device, client_ip, acc_id))
            conn.commit(); cur.close(); conn.close()
            return jsonify({'message': 'Registered successfully'}), 201

        cur.execute('SELECT 1 FROM accounts WHERE LOWER(name)=%s AND ip=%s', (name.lower(), client_ip))
        if cur.fetchone():
            cur.close(); conn.close()
            return jsonify({'error': 'Tên này đã đăng ký từ thiết bị/mạng của bạn rồi!'}), 409

        if email:
            cur.execute('SELECT 1 FROM accounts WHERE LOWER(email)=%s', (email,))
            if cur.fetchone():
                cur.close(); conn.close()
                return jsonify({'error': 'Email này đã được đăng ký!'}), 409

        cur.execute('''INSERT INTO accounts (id, name, password, email, created_at, assigned_key, assigned_at, key_expiry, device, ip)
                        VALUES (%s,%s,%s,%s,%s,NULL,NULL,NULL,%s,%s)''',
                    (acc_id, name, password, email, data.get('createdAt', int(time.time()*1000)), device, client_ip))
        conn.commit(); cur.close(); conn.close()
        return jsonify({'message': 'Registered successfully'}), 201

    else:
        existing = next((a for a in JSONDB['accounts'] if a.get('id') == acc_id), None)
        if existing:
            existing.update({'name': name, 'email': email, 'password': password, 'device': device, 'ip': client_ip})
            _save(JSONDB)
            return jsonify({'message': 'Registered successfully'}), 201

        dup_name_ip = next((a for a in JSONDB['accounts']
                             if a.get('name', '').strip().lower() == name.lower() and a.get('ip') == client_ip), None)
        if dup_name_ip:
            return jsonify({'error': 'Tên này đã đăng ký từ thiết bị/mạng của bạn rồi!'}), 409

        dup_email = next((a for a in JSONDB['accounts']
                           if a.get('email', '').strip().lower() == email and email), None)
        if dup_email:
            return jsonify({'error': 'Email này đã được đăng ký!'}), 409

        JSONDB['accounts'].append({
            'id': acc_id, 'name': name, 'password': password, 'email': email,
            'createdAt': data.get('createdAt', int(time.time()*1000)),
            'assignedKey': None, 'assignedAt': None, 'device': device, 'ip': client_ip,
        })
        _save(JSONDB)
        return jsonify({'message': 'Registered successfully'}), 201


# ─────────────────────────────────────────────────────────────
# 2. GET /accounts — Admin lấy toàn bộ danh sách tài khoản
# ─────────────────────────────────────────────────────────────
@app.route('/accounts', methods=['GET'])
def get_accounts():
    if USE_DB:
        conn = get_conn()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute('SELECT * FROM accounts ORDER BY created_at DESC')
        rows = cur.fetchall()
        cur.close(); conn.close()
        return jsonify({'accounts': [_acc_row_to_dict(r) for r in rows]}), 200
    else:
        accs = sorted(JSONDB['accounts'], key=lambda a: a.get('createdAt', 0), reverse=True)
        return jsonify({'accounts': accs}), 200


# ─────────────────────────────────────────────────────────────
# 3. POST /assign-key — Admin cấp key cho 1 tài khoản
# ─────────────────────────────────────────────────────────────
@app.route('/assign-key', methods=['POST'])
def assign_key():
    data = request.json or {}
    account_id = data.get('accountId')
    key = data.get('key')
    exp = data.get('exp')
    max_devices = int(data.get('maxDevices', 1) or 1)

    if USE_DB:
        conn = get_conn()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute('SELECT * FROM accounts WHERE id=%s', (account_id,))
        acc = cur.fetchone()
        if not acc:
            cur.close(); conn.close()
            return jsonify({'error': 'Không tìm thấy tài khoản'}), 404

        now = int(time.time()*1000)
        cur.execute('UPDATE accounts SET assigned_key=%s, assigned_at=%s, key_expiry=%s WHERE id=%s',
                    (key, now, exp, account_id))
        cur.execute('''INSERT INTO keys_store (key, exp, user_name, created, account_id, max_devices, devices)
                        VALUES (%s,%s,%s,%s,%s,%s,'[]'::jsonb)
                        ON CONFLICT (key) DO UPDATE SET exp=EXCLUDED.exp, max_devices=EXCLUDED.max_devices''',
                    (key, exp, acc['name'], now, account_id, max_devices))
        conn.commit(); cur.close(); conn.close()
        return jsonify({'message': 'Key assigned', 'key': key}), 200
    else:
        acc = next((a for a in JSONDB['accounts'] if a.get('id') == account_id), None)
        if not acc:
            return jsonify({'error': 'Không tìm thấy tài khoản'}), 404
        acc['assignedKey'] = key
        acc['assignedAt'] = int(time.time()*1000)
        acc['keyExpiry'] = exp
        JSONDB['keys'][key] = {'exp': exp, 'user': acc.get('name'), 'created': int(time.time()*1000),
                                'accountId': account_id, 'maxDevices': max_devices, 'devices': []}
        _save(JSONDB)
        return jsonify({'message': 'Key assigned', 'key': key}), 200


# ─────────────────────────────────────────────────────────────
# 3b. POST /create-key — Admin tạo key rời (không gắn tài khoản)
# ─────────────────────────────────────────────────────────────
@app.route('/create-key', methods=['POST'])
def create_key():
    data = request.json or {}
    key = data.get('key')
    if not key:
        return jsonify({'error': 'Thiếu key'}), 400
    exp = data.get('exp')
    max_devices = int(data.get('maxDevices', 1) or 1)
    user = data.get('user', '')

    if USE_DB:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute('''INSERT INTO keys_store (key, exp, user_name, created, account_id, max_devices, devices)
                        VALUES (%s,%s,%s,%s,NULL,%s,'[]'::jsonb)
                        ON CONFLICT (key) DO UPDATE SET exp=EXCLUDED.exp, max_devices=EXCLUDED.max_devices''',
                    (key, exp, user, int(time.time()*1000), max_devices))
        conn.commit(); cur.close(); conn.close()
        return jsonify({'message': 'Key created', 'key': key}), 201
    else:
        JSONDB['keys'][key] = {'exp': exp, 'user': user, 'created': int(time.time()*1000),
                                'accountId': None, 'maxDevices': max_devices, 'devices': []}
        _save(JSONDB)
        return jsonify({'message': 'Key created', 'key': key}), 201


# ─────────────────────────────────────────────────────────────
# 4. GET /my-account?id=... — User tự kiểm tra tài khoản của mình
# ─────────────────────────────────────────────────────────────
@app.route('/my-account', methods=['GET'])
def my_account():
    account_id = request.args.get('id')
    if USE_DB:
        conn = get_conn()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute('SELECT id, name, assigned_key FROM accounts WHERE id=%s', (account_id,))
        acc = cur.fetchone()
        cur.close(); conn.close()
        if not acc:
            return jsonify({'error': 'Account not found'}), 404
        return jsonify({'id': acc['id'], 'name': acc['name'], 'assignedKey': acc['assigned_key']}), 200
    else:
        acc = next((a for a in JSONDB['accounts'] if a.get('id') == account_id), None)
        if not acc:
            return jsonify({'error': 'Account not found'}), 404
        return jsonify({'id': acc['id'], 'name': acc['name'], 'assignedKey': acc.get('assignedKey')}), 200


# ─────────────────────────────────────────────────────────────
# 5. GET /inbox?device=... — Hộp thư: key đã cấp cho tài khoản
# đăng ký TỪ thiết bị này.
# ─────────────────────────────────────────────────────────────
@app.route('/inbox', methods=['GET'])
def inbox():
    device = request.args.get('device')
    out = []
    if USE_DB:
        conn = get_conn()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute('SELECT id, name, assigned_key FROM accounts WHERE device=%s AND assigned_key IS NOT NULL', (device,))
        for acc in cur.fetchall():
            out.append({'id': 'ACCKEY_'+acc['id'], 'key': acc['assigned_key'],
                        'note': '🎁 Admin đã cấp key cho tài khoản "'+acc['name']+'"', 'orderId': ''})
        cur.close(); conn.close()
    else:
        for acc in JSONDB['accounts']:
            if acc.get('device') == device and acc.get('assignedKey'):
                out.append({'id': 'ACCKEY_'+acc['id'], 'key': acc['assignedKey'],
                            'note': '🎁 Admin đã cấp key cho tài khoản "'+acc['name']+'"', 'orderId': ''})
    return jsonify({'keys': out}), 200


# ─────────────────────────────────────────────────────────────
# 6. POST /verify-key — Xác thực key khi kích hoạt / gia hạn
# ─────────────────────────────────────────────────────────────
KEY_DURATIONS = {'1GIO': 1/24, '12GIO': 0.5, '1DAY': 1, '4DAY': 4, '1TUAN': 7, '1THANG': 30}

@app.route('/verify-key', methods=['POST'])
def verify_key():
    data = request.json or {}
    key = (data.get('key') or '').strip().upper()
    device = data.get('device')
    now = int(time.time()*1000)

    if USE_DB:
        conn = get_conn()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute('SELECT * FROM keys_store WHERE key=%s', (key,))
        rec = cur.fetchone()
        if rec:
            exp = rec['exp']
            if exp and exp <= now:
                cur.close(); conn.close()
                return jsonify({'valid': False, 'error': 'Key đã hết hạn'}), 200

            max_devices = rec['max_devices'] or 1
            devices = rec['devices'] or []
            if device and device not in devices:
                if len(devices) >= max_devices:
                    cur.close(); conn.close()
                    return jsonify({'valid': False, 'error': 'Key đã đạt giới hạn '+str(max_devices)+' thiết bị!'}), 200
                devices.append(device)
                cur.execute('UPDATE keys_store SET devices=%s::jsonb WHERE key=%s', (json.dumps(devices), key))
                conn.commit()

            days_left = max((exp - now)/86400000, 0) if exp else 36500
            cur.close(); conn.close()
            return jsonify({'valid': True, 'days': days_left, 'devicesUsed': len(devices), 'maxDevices': max_devices}), 200
        cur.close(); conn.close()
    else:
        rec = JSONDB['keys'].get(key)
        if rec:
            exp = rec.get('exp')
            if exp and exp <= now:
                return jsonify({'valid': False, 'error': 'Key đã hết hạn'}), 200
            max_devices = int(rec.get('maxDevices', 1) or 1)
            devices = rec.get('devices')
            if devices is None:
                devices = [rec['device']] if rec.get('device') else []
                rec['devices'] = devices
            if device and device not in devices:
                if len(devices) >= max_devices:
                    return jsonify({'valid': False, 'error': 'Key đã đạt giới hạn '+str(max_devices)+' thiết bị!'}), 200
                devices.append(device)
                _save(JSONDB)
            days_left = max((exp - now)/86400000, 0) if exp else 36500
            return jsonify({'valid': True, 'days': days_left, 'devicesUsed': len(devices), 'maxDevices': max_devices}), 200

    # Không có trong kho -> chỉ kiểm tra định dạng (key rất cũ / chưa đồng bộ)
    m = re.match(r'^SHADOW-([A-Z0-9]+)-[A-Z0-9]+$', key)
    if m:
        days = KEY_DURATIONS.get(m.group(1), 1)
        return jsonify({'valid': True, 'days': days}), 200
    return jsonify({'valid': False, 'error': 'Key sai định dạng'}), 200


# ─────────────────────────────────────────────────────────────
# 7. GET/POST /api/status — Trạng thái bảo trì
# ─────────────────────────────────────────────────────────────
@app.route('/api/status', methods=['GET'])
def api_status():
    diag = {
        'storage': 'postgres' if (USE_DB and not DB_ERROR) else ('postgres-error' if USE_DB else 'json-fallback (KHONG BEN VUNG)'),
        'dbError': DB_ERROR
    }
    if USE_DB and not DB_ERROR:
        conn = get_conn()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute('SELECT locked, message FROM settings WHERE id=1')
        row = cur.fetchone()
        cur.execute('SELECT COUNT(*) as c FROM accounts')
        cnt = cur.fetchone()
        cur.close(); conn.close()
        diag['accountCount'] = cnt['c']
        return jsonify(dict({'locked': row['locked'], 'message': row['message']}, **diag)), 200
    else:
        data = JSONDB.get('maintenance', {'locked': False, 'message': ''}) if not USE_DB else {'locked': False, 'message': ''}
        return jsonify(dict(data, **diag)), 200

@app.route('/api/status', methods=['POST'])
def set_status():
    data = request.json or {}
    locked = bool(data.get('locked', False))
    message = data.get('message', '')
    if USE_DB:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute('UPDATE settings SET locked=%s, message=%s WHERE id=1', (locked, message))
        conn.commit(); cur.close(); conn.close()
    else:
        JSONDB['maintenance'] = {'locked': locked, 'message': message}
        _save(JSONDB)
    return jsonify({'locked': locked, 'message': message}), 200


# ─────────────────────────────────────────────────────────────
# 7b. GET/POST /pricing — Giá key + sale (đồng bộ server để trang
# admin riêng có thể chỉnh và tool chính tự cập nhật theo)
# ─────────────────────────────────────────────────────────────
@app.route('/pricing', methods=['GET'])
def get_pricing():
    if USE_DB and not DB_ERROR:
        conn = get_conn()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute('SELECT base_price, sale_price, sale_expiry FROM settings WHERE id=1')
        row = cur.fetchone()
        cur.close(); conn.close()
        return jsonify({'base': row['base_price'], 'sale': row['sale_price'], 'saleExpiry': row['sale_expiry']}), 200
    else:
        p = JSONDB.get('pricing', {'base': 33000, 'sale': 0, 'saleExpiry': None}) if not USE_DB else {'base': 33000, 'sale': 0, 'saleExpiry': None}
        return jsonify(p), 200

@app.route('/pricing', methods=['POST'])
def set_pricing():
    data = request.json or {}
    base = int(data.get('base', 33000) or 33000)
    sale = int(data.get('sale', 0) or 0)
    sale_expiry = data.get('saleExpiry')
    if USE_DB and not DB_ERROR:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute('UPDATE settings SET base_price=%s, sale_price=%s, sale_expiry=%s WHERE id=1', (base, sale, sale_expiry))
        conn.commit(); cur.close(); conn.close()
    else:
        JSONDB['pricing'] = {'base': base, 'sale': sale, 'saleExpiry': sale_expiry}
        _save(JSONDB)
    return jsonify({'base': base, 'sale': sale, 'saleExpiry': sale_expiry}), 200


# ─────────────────────────────────────────────────────────────
# 7c. GET /keys — Admin lấy toàn bộ danh sách key đã tạo/cấp
# ─────────────────────────────────────────────────────────────
@app.route('/keys', methods=['GET'])
def get_keys():
    if USE_DB and not DB_ERROR:
        conn = get_conn()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute('SELECT * FROM keys_store ORDER BY created DESC')
        rows = cur.fetchall()
        cur.close(); conn.close()
        keys = [{'key': r['key'], 'exp': r['exp'], 'user': r['user_name'], 'created': r['created'],
                 'accountId': r['account_id'], 'maxDevices': r['max_devices'], 'devices': r['devices']} for r in rows]
        return jsonify({'keys': keys}), 200
    else:
        keys = [dict(v, key=k) for k, v in JSONDB.get('keys', {}).items()] if not USE_DB else []
        keys.sort(key=lambda x: x.get('created', 0), reverse=True)
        return jsonify({'keys': keys}), 200


# ─────────────────────────────────────────────────────────────
# 7d. POST /delete-key — Admin xoá 1 key khỏi kho
# ─────────────────────────────────────────────────────────────
@app.route('/delete-key', methods=['POST'])
def delete_key():
    data = request.json or {}
    key = data.get('key')
    if USE_DB and not DB_ERROR:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute('DELETE FROM keys_store WHERE key=%s', (key,))
        deleted = cur.rowcount
        conn.commit(); cur.close(); conn.close()
        if not deleted:
            return jsonify({'error': 'Không tìm thấy key'}), 404
        return jsonify({'message': 'Deleted'}), 200
    else:
        if key in JSONDB.get('keys', {}):
            del JSONDB['keys'][key]
            _save(JSONDB)
            return jsonify({'message': 'Deleted'}), 200
        return jsonify({'error': 'Không tìm thấy key'}), 404


# ─────────────────────────────────────────────────────────────
# 8. POST /login — Đăng nhập bằng email + mật khẩu
# ─────────────────────────────────────────────────────────────
@app.route('/login', methods=['POST'])
def login():
    data = request.json or {}
    email = data.get('email')
    password = data.get('password')

    if USE_DB:
        conn = get_conn()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute('SELECT * FROM accounts WHERE email=%s AND password=%s', (email, password))
        acc = cur.fetchone()
        cur.close(); conn.close()
        if acc:
            return jsonify({'success': True, 'account': _acc_row_to_dict(acc)}), 200
        return jsonify({'success': False, 'error': 'Sai email hoặc mật khẩu'}), 200
    else:
        for acc in JSONDB['accounts']:
            if acc.get('email') == email and acc.get('password') == password:
                return jsonify({'success': True, 'account': acc}), 200
        return jsonify({'success': False, 'error': 'Sai email hoặc mật khẩu'}), 200


# ─────────────────────────────────────────────────────────────
# 9. POST /delete-account — Admin xoá 1 tài khoản
# Đây là CÁCH DUY NHẤT 1 tài khoản bị mất — admin chủ động xoá.
# ─────────────────────────────────────────────────────────────
@app.route('/delete-account', methods=['POST'])
def delete_account():
    data = request.json or {}
    account_id = data.get('accountId')

    if USE_DB:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute('DELETE FROM accounts WHERE id=%s', (account_id,))
        deleted = cur.rowcount
        conn.commit(); cur.close(); conn.close()
        if not deleted:
            return jsonify({'error': 'Không tìm thấy tài khoản'}), 404
        return jsonify({'message': 'Deleted'}), 200
    else:
        before = len(JSONDB['accounts'])
        JSONDB['accounts'] = [a for a in JSONDB['accounts'] if a.get('id') != account_id]
        if len(JSONDB['accounts']) == before:
            return jsonify({'error': 'Không tìm thấy tài khoản'}), 404
        _save(JSONDB)
        return jsonify({'message': 'Deleted'}), 200


# ─────────────────────────────────────────────────────────────
# 7e. GET/POST /bank-config — Thông tin ngân hàng nhận tiền,
# dùng để tool chính tạo VietQR động thay vì hardcode.
# ─────────────────────────────────────────────────────────────
@app.route('/bank-config', methods=['GET'])
def get_bank_config():
    if USE_DB and not DB_ERROR:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("ALTER TABLE settings ADD COLUMN IF NOT EXISTS bank_code TEXT")
        cur.execute("ALTER TABLE settings ADD COLUMN IF NOT EXISTS bank_name TEXT")
        cur.execute("ALTER TABLE settings ADD COLUMN IF NOT EXISTS bank_stk TEXT")
        cur.execute("ALTER TABLE settings ADD COLUMN IF NOT EXISTS bank_account_name TEXT")
        conn.commit()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute('SELECT bank_code, bank_name, bank_stk, bank_account_name FROM settings WHERE id=1')
        row = cur.fetchone()
        cur.close(); conn.close()
        return jsonify({'bankCode': row['bank_code'], 'bankName': row['bank_name'], 'stk': row['bank_stk'], 'accountName': row['bank_account_name']}), 200
    else:
        b = JSONDB.get('bankConfig', {}) if not USE_DB else {}
        return jsonify(b), 200

@app.route('/bank-config', methods=['POST'])
def set_bank_config():
    data = request.json or {}
    bank_code = data.get('bankCode')
    bank_name = data.get('bankName')
    stk = data.get('stk')
    account_name = data.get('accountName')
    if USE_DB and not DB_ERROR:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("ALTER TABLE settings ADD COLUMN IF NOT EXISTS bank_code TEXT")
        cur.execute("ALTER TABLE settings ADD COLUMN IF NOT EXISTS bank_name TEXT")
        cur.execute("ALTER TABLE settings ADD COLUMN IF NOT EXISTS bank_stk TEXT")
        cur.execute("ALTER TABLE settings ADD COLUMN IF NOT EXISTS bank_account_name TEXT")
        cur.execute('UPDATE settings SET bank_code=%s, bank_name=%s, bank_stk=%s, bank_account_name=%s WHERE id=1',
                    (bank_code, bank_name, stk, account_name))
        conn.commit(); cur.close(); conn.close()
    else:
        JSONDB['bankConfig'] = {'bankCode': bank_code, 'bankName': bank_name, 'stk': stk, 'accountName': account_name}
        _save(JSONDB)
    return jsonify({'bankCode': bank_code, 'bankName': bank_name, 'stk': stk, 'accountName': account_name}), 200


# ─────────────────────────────────────────────────────────────
# /api/game-config — Bật/tắt từng game + tính năng phân tích/link game
# GET  → trả về config hiện tại
# POST → {game: 'sunwin', enabled: true} hoặc {feature: 'analysis', enabled: false}
# ─────────────────────────────────────────────────────────────
DEFAULT_GAME_CONFIG = {
    'games': {
        'sunwin':  True,
        '68game':  True,
        'lc79':    True,
        'hitclub': True,
        'sao789':  True,
    },
    'features': {
        'analysis':  True,
        'gamelinks': True,
    }
}

@app.route('/api/game-config', methods=['GET'])
def get_game_config():
    if USE_DB and not DB_ERROR:
        conn = get_conn()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        # Lưu trong bảng settings dưới key game_config (JSON string)
        cur.execute("SELECT value FROM kv_store WHERE key='game_config'")
        row = cur.fetchone()
        cur.close(); conn.close()
        if row:
            import json as _json
            return jsonify(_json.loads(row['value'])), 200
    # Fallback: JSON file
    cfg = JSONDB.get('game_config', DEFAULT_GAME_CONFIG)
    return jsonify(cfg), 200

@app.route('/api/game-config', methods=['POST'])
def set_game_config():
    import json as _json, copy
    data = request.json or {}

    # Load current config
    if USE_DB and not DB_ERROR:
        conn = get_conn()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT value FROM kv_store WHERE key='game_config'")
        row = cur.fetchone()
        cur.close(); conn.close()
        cfg = _json.loads(row['value']) if row else copy.deepcopy(DEFAULT_GAME_CONFIG)
    else:
        cfg = JSONDB.get('game_config', copy.deepcopy(DEFAULT_GAME_CONFIG))

    # Apply change
    if 'game' in data:
        if 'games' not in cfg:
            cfg['games'] = {}
        cfg['games'][data['game']] = bool(data.get('enabled', True))
    elif 'feature' in data:
        if 'features' not in cfg:
            cfg['features'] = {}
        cfg['features'][data['feature']] = bool(data.get('enabled', True))
    else:
        return jsonify({'ok': False, 'error': 'Thiếu field game hoặc feature'}), 400

    # Save
    if USE_DB and not DB_ERROR:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO kv_store(key,value) VALUES('game_config',%s) "
            "ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value",
            (_json.dumps(cfg),)
        )
        conn.commit(); cur.close(); conn.close()
    else:
        JSONDB['game_config'] = cfg
        _save(JSONDB)

    return jsonify({'ok': True, 'config': cfg}), 200


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
