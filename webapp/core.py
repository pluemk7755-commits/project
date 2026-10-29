"""Roo Nai Gor Dai web booking core: SQLite, Thai timestamps, manual slip verification.

No payment is considered successful until a staff ADMIN verifies it against their
bank account.  Uploaded slips alone are not proof of funds.
"""
from __future__ import annotations
import hashlib, hmac, secrets, sqlite3, os, re, smtplib, ssl
from email.message import EmailMessage
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
DATA = Path(os.environ.get('RNGD_WEB_DATA', BASE/'data')).resolve()
DB_PATH = DATA/'rngd_web.sqlite3'
UPLOAD_DIR = DATA/'uploads'
TZ = timezone(timedelta(hours=7))
TERMS = ('SNOOKER','VIP','POOL')

class RuleError(Exception): pass

def now(): return datetime.now(TZ)
def moment(): return now().strftime('%Y-%m-%d %H:%M:%S')
def dstr(d): return d.strftime('%Y-%m-%d %H:%M:%S')
def parse_time(value): return datetime.strptime(value,'%Y-%m-%d %H:%M:%S').replace(tzinfo=TZ)

def password_ok(p): return isinstance(p,str) and 6 <= len(p) <= 60

def hash_password(p):
    if not password_ok(p): raise RuleError('รหัสผ่านต้องยาว 6–60 ตัวอักษร')
    salt=secrets.token_bytes(16); rounds=310000
    digest=hashlib.pbkdf2_hmac('sha256',p.encode(),salt,rounds)
    return f'pbkdf2_sha256${rounds}${salt.hex()}${digest.hex()}'

def verify_password(p,stored):
    try:
        algo,iterations,salt,digest=stored.split('$')
        if algo!='pbkdf2_sha256' or not (100000 <= int(iterations) <= 2000000):return False
        hashed=hashlib.pbkdf2_hmac('sha256',p.encode(),bytes.fromhex(salt),int(iterations))
        return hmac.compare_digest(hashed,bytes.fromhex(digest))
    except (ValueError,TypeError,AttributeError): return False

SCHEMA='''
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS staff_users(staff_id INTEGER PRIMARY KEY,username TEXT NOT NULL UNIQUE,password_hash TEXT NOT NULL,role TEXT NOT NULL CHECK(role IN ('ADMIN','STAFF')),is_active INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS users(user_id INTEGER PRIMARY KEY,username TEXT NOT NULL UNIQUE,email TEXT NOT NULL UNIQUE,phone TEXT NOT NULL UNIQUE,full_name TEXT NOT NULL,password_hash TEXT NOT NULL,is_active INTEGER NOT NULL DEFAULT 1,created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS club_tables(table_id INTEGER PRIMARY KEY,table_code TEXT NOT NULL UNIQUE,table_type TEXT NOT NULL,hourly_rate_sat INTEGER NOT NULL,status TEXT NOT NULL DEFAULT 'OPEN');
CREATE TABLE IF NOT EXISTS cues(cue_id INTEGER PRIMARY KEY,brand TEXT NOT NULL,model TEXT NOT NULL,price_sat INTEGER NOT NULL,img TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'AVAILABLE');
CREATE TABLE IF NOT EXISTS bookings(booking_id INTEGER PRIMARY KEY,user_id INTEGER NOT NULL REFERENCES users(user_id),table_id INTEGER NOT NULL REFERENCES club_tables(table_id),cue_id INTEGER REFERENCES cues(cue_id),start_at TEXT NOT NULL,end_at TEXT NOT NULL,actual_check_in_at TEXT,status TEXT NOT NULL DEFAULT 'PENDING_SLIP',table_fee_sat INTEGER NOT NULL,cue_fee_sat INTEGER NOT NULL DEFAULT 0,discount_sat INTEGER NOT NULL DEFAULT 0,total_sat INTEGER NOT NULL,credit_sat INTEGER NOT NULL DEFAULT 0,due_sat INTEGER NOT NULL,hold_expires_at TEXT NOT NULL,created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE INDEX IF NOT EXISTS idx_booking_table ON bookings(table_id,start_at,end_at);
CREATE TABLE IF NOT EXISTS booking_cues(booking_id INTEGER NOT NULL REFERENCES bookings(booking_id) ON DELETE CASCADE,cue_id INTEGER NOT NULL REFERENCES cues(cue_id),price_sat INTEGER NOT NULL,PRIMARY KEY(booking_id,cue_id));
CREATE TABLE IF NOT EXISTS payment_slips(slip_id INTEGER PRIMARY KEY,booking_id INTEGER NOT NULL REFERENCES bookings(booking_id),user_id INTEGER NOT NULL REFERENCES users(user_id),file_name TEXT NOT NULL,amount_sat INTEGER NOT NULL,status TEXT NOT NULL DEFAULT 'PENDING',uploaded_at TEXT NOT NULL,reviewer_id INTEGER REFERENCES staff_users(staff_id),reviewed_at TEXT,review_note TEXT);
CREATE INDEX IF NOT EXISTS idx_slip_booking ON payment_slips(booking_id,status);
CREATE TABLE IF NOT EXISTS credit_ledger(id INTEGER PRIMARY KEY,user_id INTEGER NOT NULL REFERENCES users(user_id),booking_id INTEGER REFERENCES bookings(booking_id),kind TEXT NOT NULL CHECK(kind IN ('NO_SHOW','USE','RETURN')),delta_sat INTEGER NOT NULL,created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,UNIQUE(booking_id,kind));
CREATE TABLE IF NOT EXISTS points_ledger(id INTEGER PRIMARY KEY,user_id INTEGER NOT NULL REFERENCES users(user_id),booking_id INTEGER NOT NULL UNIQUE REFERENCES bookings(booking_id),points INTEGER NOT NULL,created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS points_wallet_ledger(id INTEGER PRIMARY KEY,user_id INTEGER NOT NULL REFERENCES users(user_id),booking_id INTEGER NOT NULL REFERENCES bookings(booking_id),kind TEXT NOT NULL CHECK(kind IN ('REDEEM','RETURN')),delta_points INTEGER NOT NULL,created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,UNIQUE(booking_id,kind));
CREATE TABLE IF NOT EXISTS password_reset(user_id INTEGER PRIMARY KEY REFERENCES users(user_id) ON DELETE CASCADE,hash TEXT NOT NULL,expires_at TEXT NOT NULL,requested_at TEXT NOT NULL,attempts INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS login_throttle(identifier_hash TEXT PRIMARY KEY,attempts INTEGER NOT NULL DEFAULT 0,locked_until TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS reset_grants(session_hash TEXT PRIMARY KEY,user_id INTEGER NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,expires_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS auth_sessions(token_hash TEXT PRIMARY KEY,csrf TEXT NOT NULL,user_id INTEGER REFERENCES users(user_id),staff_id INTEGER REFERENCES staff_users(staff_id),expires_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS audit_logs(id INTEGER PRIMARY KEY,staff_id INTEGER REFERENCES staff_users(staff_id),event TEXT NOT NULL,entity_type TEXT NOT NULL,entity_id INTEGER,note TEXT NOT NULL DEFAULT '',created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS settings(setting_key TEXT PRIMARY KEY,setting_value TEXT NOT NULL);
'''

@contextmanager
def connect(write=False):
    DATA.mkdir(parents=True,exist_ok=True); UPLOAD_DIR.mkdir(parents=True,exist_ok=True)
    c=sqlite3.connect(str(DB_PATH),timeout=12, isolation_level=None)
    c.row_factory=sqlite3.Row
    c.execute('PRAGMA foreign_keys=ON')
    c.execute('PRAGMA busy_timeout=12000')
    try:
        if write:c.execute('BEGIN IMMEDIATE')
        yield c
        if write:c.commit()
    except Exception:
        if write:c.rollback()
        raise
    finally:c.close()

def init_db():
    DATA.mkdir(parents=True,exist_ok=True)
    with connect() as c:c.executescript(SCHEMA)
    # Additive migration: retain bookings and the original ADMIN database as-is.
    with connect(True) as c:
        existing={row['name'] for row in c.execute('PRAGMA table_info(bookings)')}
        for name,kind in (('redeemed_points','INTEGER NOT NULL DEFAULT 0'),('points_discount_sat','INTEGER NOT NULL DEFAULT 0')):
            if name not in existing:c.execute('ALTER TABLE bookings ADD COLUMN '+name+' '+kind)
    with connect(True) as c:
        table_rows=[(f'Snooker {i:02}','SNOOKER',12000) for i in range(1,7)] + [('Snooker VIP 01','VIP',15000)] + [(f'Pool {i:02}','POOL',10000) for i in range(1,3)]
        c.executemany('INSERT OR IGNORE INTO club_tables(table_code,table_type,hourly_rate_sat) VALUES(?,?,?)',table_rows)
        # Balanced retail-style rental pricing (THB per booking), owner-adjustable.
        # Retail purchase figures are planning estimates; bookings already made retain their snapshot prices.
        _sync_exact_cues(c)
        c.execute("INSERT OR IGNORE INTO settings(setting_key,setting_value) VALUES('grace_minutes','15')")
        c.execute("INSERT OR IGNORE INTO settings(setting_key,setting_value) VALUES('daytime_promotion','1')")


# Maintain EXACTLY the selected 10 rentable models. Existing booking history is never deleted.
_CANONICAL_CUES = [('Predator', 'REVO 12.4', 20000, 'cue_predator.png'), ('Cuetec', 'Cynergy 12.5', 18000, 'cue_cuetec.png'), ('Mezz', 'EC9', 19000, 'cue_mezz.png'), ('Peradon', 'Classic', 13000, 'cue_peradon.png'), ('Predator', 'Aspire', 10000, 'cue_aspire.png'), ("O'Min", "O'Millenium 3/4", 8000, 'cue_omin_millenium_distinct.webp'), ("O'Min", 'Professional 3/4', 13000, 'cue_omin_professional_distinct.webp'), ("O'Min", 'Perfect 3/4', 17000, 'cue_omin_perfect_distinct.webp'), ('PowerGlide', 'Asas 3/4', 11000, 'cue_powerglide_asas_distinct.webp'), ('Peradon', 'Crown 3/4', 14000, 'cue_peradon_crown_distinct.webp')]
_CUE_MIGRATION_KEY = 'cue_exact10_v4_pricing'
def _sync_exact_cues(c):
    desired={(brand,model):(price,photo) for brand,model,price,photo in _CANONICAL_CUES}
    marker=c.execute('SELECT 1 FROM settings WHERE setting_key=?',(_CUE_MIGRATION_KEY,)).fetchone()
    old=c.execute('SELECT cue_id, brand, model, price_sat, img, status FROM cues ORDER BY cue_id').fetchall()
    seen=set()
    for row in old:
        cue_id,brand,model=row['cue_id'],row['brand'],row['model']
        key=(brand,model)
        if key in desired and key not in seen:
            seen.add(key)
            price,photo=desired[key]
            if marker:
                c.execute('UPDATE cues SET img=?,status="AVAILABLE" WHERE cue_id=?',(photo,cue_id))
            else:
                c.execute('UPDATE cues SET price_sat=?,img=?,status="AVAILABLE" WHERE cue_id=?',(price,photo,cue_id))
            continue
        # A duplicate, obsolete model or unknown legacy item must not be bookable.
        # Keep a historical row only when an existing booking references it.
        used=c.execute('SELECT 1 FROM bookings WHERE cue_id=? LIMIT 1',(cue_id,)).fetchone() or c.execute('SELECT 1 FROM booking_cues WHERE cue_id=? LIMIT 1',(cue_id,)).fetchone()
        if used:
            c.execute('UPDATE cues SET status="RETIRED" WHERE cue_id=?',(cue_id,))
        else:
            c.execute('DELETE FROM cues WHERE cue_id=?',(cue_id,))
    for brand,model,price,photo in _CANONICAL_CUES:
        if (brand,model) not in seen:
            c.execute('INSERT INTO cues(brand,model,price_sat,img,status) VALUES(?,?,?, ?,"AVAILABLE")',(brand,model,price,photo))
    if not marker:
        c.execute('INSERT INTO settings(setting_key,setting_value) VALUES(?,?)',(_CUE_MIGRATION_KEY,'applied'))

def create_session(user_id=None,staff_id=None):
    token=secrets.token_urlsafe(36); csrf=secrets.token_urlsafe(24)
    with connect(True) as c:
        c.execute('DELETE FROM auth_sessions WHERE expires_at<?',(moment(),))
        c.execute('INSERT INTO auth_sessions VALUES (?,?,?,?,?)',(hashlib.sha256(token.encode()).hexdigest(),csrf,user_id,staff_id,dstr(now()+timedelta(hours=6))))
    return token,csrf

def get_session(token):
    if not token:return None
    hashed=hashlib.sha256(token.encode()).hexdigest()
    with connect() as c:
        row=c.execute('''SELECT s.*,u.full_name,u.is_active AS user_active,st.role,st.username AS staff_name,st.is_active AS staff_active
           FROM auth_sessions s LEFT JOIN users u ON u.user_id=s.user_id LEFT JOIN staff_users st ON st.staff_id=s.staff_id
           WHERE token_hash=? AND expires_at>?''',(hashed,moment())).fetchone()
    if not row or (row['user_id'] and not row['user_active']) or (row['staff_id'] and not row['staff_active']):return None
    return dict(row)

def end_session(token):
    if token:
        with connect(True) as c:c.execute('DELETE FROM auth_sessions WHERE token_hash=?',(hashlib.sha256(token.encode()).hexdigest(),))

def register(name,email,phone,password):
    email=email.strip().lower();phone=phone.strip();name=name.strip()
    if not 2 <= len(name) <= 100 or not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+',email) or not re.fullmatch(r'[+0-9\-]{9,16}',phone):raise RuleError('กรุณากรอกชื่อ อีเมล และเบอร์โทรให้ถูกต้อง')
    if not password_ok(password):raise RuleError('รหัสผ่านต้องยาว 6–60 ตัวอักษร (ใช้ตัวอักษรแบบใดก็ได้)')
    with connect(True) as c:
        try:
            cur=c.execute('INSERT INTO users(username,email,phone,full_name,password_hash) VALUES(?,?,?,?,?)',('member_'+secrets.token_hex(6),email,phone,name,hash_password(password)))
        except sqlite3.IntegrityError:raise RuleError('อีเมลหรือเบอร์โทรนี้สมัครสมาชิกแล้ว')
    return cur.lastrowid

def profile_details(uid):
    with connect() as c:
        row=c.execute('SELECT user_id,full_name,email,phone FROM users WHERE user_id=? AND is_active=1',(uid,)).fetchone()
        if not row:raise RuleError('ไม่พบบัญชีผู้ใช้')
        return dict(row)

def update_profile(uid,name,email,phone,password):
    name=name.strip();email=email.strip().lower();phone=phone.strip()
    if not (2<=len(name)<=100) or not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+',email) or not re.fullmatch(r'[+0-9\-]{9,16}',phone):
        raise RuleError('กรุณากรอกข้อมูลให้ถูกต้อง')
    if not password:raise RuleError('กรุณากรอกรหัสผ่านปัจจุบันเพื่อยืนยัน')
    with connect(True) as c:
        row=c.execute('SELECT password_hash,email FROM users WHERE user_id=? AND is_active=1',(uid,)).fetchone()
        if not row or not verify_password(password,row['password_hash']):raise RuleError('รหัสผ่านปัจจุบันไม่ถูกต้อง')
        if email!=row['email']:
            raise RuleError('การเปลี่ยนอีเมลต้องยืนยันอีเมลใหม่ก่อน ยังไม่เปิดให้เปลี่ยนผ่านหน้านี้')
        try:c.execute('UPDATE users SET full_name=?,phone=? WHERE user_id=?',(name,phone,uid))
        except sqlite3.IntegrityError:raise RuleError('เบอร์โทรนี้ถูกใช้แล้ว')

def _throttle_key(kind,identifier):
    return hashlib.sha256((kind+':'+identifier.casefold().strip()).encode()).hexdigest()

def _throttle_check(kind,identifier):
    key=_throttle_key(kind,identifier)
    with connect() as c:row=c.execute('SELECT attempts,locked_until FROM login_throttle WHERE identifier_hash=?',(key,)).fetchone()
    if row and row['attempts']>=5 and row['locked_until']>moment():raise RuleError('เข้าสู่ระบบผิดหลายครั้ง กรุณารอ 15 นาที')
    return key

def _throttle_result(key,success):
    with connect(True) as c:
        if success:c.execute('DELETE FROM login_throttle WHERE identifier_hash=?',(key,));return
        current=c.execute('SELECT attempts,locked_until FROM login_throttle WHERE identifier_hash=?',(key,)).fetchone()
        tries=(current['attempts'] if current and current['locked_until']>moment() else 0)+1
        c.execute('INSERT INTO login_throttle(identifier_hash,attempts,locked_until) VALUES(?,?,?) ON CONFLICT(identifier_hash) DO UPDATE SET attempts=excluded.attempts,locked_until=excluded.locked_until',(key,tries,dstr(now()+timedelta(minutes=15))))

def login(identifier,password):
    identifier=identifier.strip()
    key=_throttle_check('customer',identifier)
    with connect() as c:
        # Use unique credentials first. Full names are not unique, so only
        # allow a name login when it belongs to exactly one active account.
        row=c.execute('''SELECT user_id,password_hash,is_active FROM users
                         WHERE lower(email)=lower(?) OR phone=? OR username=?''',
                      (identifier,identifier,identifier)).fetchone()
        if not row:
            names=c.execute('''SELECT user_id,password_hash,is_active FROM users
                               WHERE full_name=? AND is_active=1 LIMIT 2''',(identifier,)).fetchall()
            if len(names)>1:
                # Do not guess which account a duplicate real-world name refers to.
                raise RuleError('พบชื่อ-นามสกุลซ้ำ กรุณาเข้าสู่ระบบด้วยอีเมลหรือเบอร์โทร')
            row=names[0] if names else None
    if not row or row['is_active']!=1 or not verify_password(password,row['password_hash']):
        _throttle_result(key,False);raise RuleError('ข้อมูลเข้าสู่ระบบหรือรหัสผ่านไม่ถูกต้อง')
    _throttle_result(key,True)
    return row['user_id']

def login_staff(identifier,password):
    key=_throttle_check('staff',identifier)
    with connect() as c:row=c.execute('SELECT staff_id,password_hash,is_active FROM staff_users WHERE username=?',(identifier.strip(),)).fetchone()
    if not row or row['is_active']!=1 or not verify_password(password,row['password_hash']):
        _throttle_result(key,False);raise RuleError('บัญชีผู้ดูแลหรือรหัสผ่านไม่ถูกต้อง')
    _throttle_result(key,True)
    return row['staff_id']

def membership(c,uid):
    lifetime=c.execute('SELECT COALESCE(SUM(points),0) FROM points_ledger WHERE user_id=?',(uid,)).fetchone()[0]
    name,discount='Bronze',0
    for points,tier,bps in [(1000,'Silver',500),(2500,'Gold',1000),(5000,'VIP',1500)]:
        if lifetime>=points:name,discount=tier,bps
    spent=c.execute('SELECT COALESCE(SUM(delta_points),0) FROM points_wallet_ledger WHERE user_id=?',(uid,)).fetchone()[0]
    return {'name':name,'points':lifetime,'available_points':max(0,lifetime+spent),'discount_bps':discount}

def credit_balance(c, uid):
    return c.execute('SELECT COALESCE(SUM(delta_sat),0) FROM credit_ledger WHERE user_id=?',(uid,)).fetchone()[0]

def normalize_cue_ids(cue_id=None,cue_ids=None):
    ids=list(cue_ids or []) if cue_ids is not None else ([cue_id] if cue_id else [])
    try: ids=[int(x) for x in ids if str(x).strip()]
    except (ValueError,TypeError):raise RuleError('ข้อมูลไม้คิวไม่ถูกต้อง')
    if len(ids)!=len(set(ids)) or any(x<=0 for x in ids):raise RuleError('ไม่สามารถเลือกไม้คิวซ้ำหรือใช้รหัสไม้คิวไม่ถูกต้อง')
    return ids

def quote(c,table_id,start_at,hours,cue_id=None,discount=True,user_id=None,cue_ids=None,redeem_points=0):
    table=c.execute('SELECT * FROM club_tables WHERE table_id=? AND status="OPEN"',(table_id,)).fetchone()
    if not table:raise RuleError('โต๊ะนี้ไม่พร้อมให้จอง')
    if not 1 <= hours <= 6:raise RuleError('เลือกจองได้ 1–6 ชั่วโมง')
    start=parse_time(start_at); end=start+timedelta(hours=hours)
    if start < now()-timedelta(seconds=60):raise RuleError('ไม่สามารถจองย้อนหลังได้')
    # Open 10:00 until 02:00 next day
    base=start.date() if start.hour >= 10 else start.date()-timedelta(days=1)
    shop_open=datetime.combine(base,datetime.min.time(),TZ)+timedelta(hours=10)
    shop_close=shop_open+timedelta(hours=16)
    if not (shop_open<=start<end<=shop_close):raise RuleError('ช่วงเวลาจองต้องอยู่ระหว่าง 10:00–02:00 น.')
    active=('PENDING_SLIP','UNDER_REVIEW','CONFIRMED','CHECKED_IN')
    overlaps=c.execute(f'''SELECT 1 FROM bookings WHERE table_id=? AND start_at<? AND end_at>? AND status IN ({','.join('?'*len(active))}) AND (status!='PENDING_SLIP' OR hold_expires_at>?) LIMIT 1''',(table_id,dstr(end),start_at,*active,moment())).fetchone()
    if overlaps:raise RuleError('ช่วงเวลานี้มีการจองโต๊ะแล้ว กรุณาเลือกเวลาอื่น')
    selected=normalize_cue_ids(cue_id,cue_ids)
    cue_items=[]
    for cid in selected:
        cue=c.execute('SELECT * FROM cues WHERE cue_id=? AND status="AVAILABLE"',(cid,)).fetchone()
        if not cue:raise RuleError('ไม้คิวไม่พร้อมให้เช่า')
        clash=c.execute('''''''''SELECT 1 FROM bookings b
            LEFT JOIN booking_cues bc ON bc.booking_id=b.booking_id
            WHERE (bc.cue_id=? OR (b.cue_id=? AND bc.booking_id IS NULL))
              AND b.start_at<? AND b.end_at>?
              AND (b.status IN ('UNDER_REVIEW','CONFIRMED','CHECKED_IN','PENDING_SLIP'))
              AND (b.status!='PENDING_SLIP' OR b.hold_expires_at>?) LIMIT 1''''''''' ,(cid,cid,dstr(end),start_at,moment())).fetchone()
        if clash:raise RuleError('ไม้คิวที่เลือกมีผู้จองในช่วงเวลานี้แล้ว')
        cue_items.append(dict(cue))
    rate=table['hourly_rate_sat']; fee=rate*hours; discount_sat=0
    if discount and table['table_type'] in TERMS:
        special={'SNOOKER':9000,'VIP':12000,'POOL':8000}[table['table_type']]
        cursor=start
        while cursor < end:
            slot_end=min(cursor+timedelta(minutes=30),end)
            if 10 <= cursor.hour < 16:
                discount_sat+=int((rate-special)*(slot_end-cursor).total_seconds()/3600)
            cursor=slot_end
    cue_fee=sum(row['price_sat'] for row in cue_items)
    member_bps=membership(c,user_id)['discount_bps'] if user_id else 0
    member_discount=max(0,fee-discount_sat)*member_bps//10000
    full=max(0,fee-discount_sat-member_discount)+cue_fee
    if redeem_points<0 or redeem_points%500 or (redeem_points and not user_id):raise RuleError('แลกคะแนนได้ครั้งละ 500 คะแนนเท่านั้น')
    available=membership(c,user_id)['available_points'] if user_id else 0
    point_discount=redeem_points//500*5000
    if redeem_points>available or point_discount>full:raise RuleError('คะแนนไม่พอ หรือส่วนลดเกินมูลค่ารายการ')
    total=full-point_discount
    return {'table':dict(table),'start':start,'end':end,'table_fee_sat':fee,'discount_sat':discount_sat,
            'member_discount_sat':member_discount,'cue_fee_sat':cue_fee,'points_discount_sat':point_discount,'redeemed_points':redeem_points,'total_sat':total,'cues':cue_items}

def book(uid,table_id,start_at,hours,cue_id=None,credit_sat=0,cue_ids=None,redeem_points=0):
    with connect(True) as c:
        q=quote(c,table_id,start_at,hours,cue_id,user_id=uid,cue_ids=cue_ids,redeem_points=redeem_points)
        if credit_sat < 0 or credit_sat>q['total_sat'] or credit_sat>credit_balance(c,uid):raise RuleError('ยอด Booking Credit ไม่เพียงพอ')
        due=q['total_sat']-credit_sat
        status='CONFIRMED' if due == 0 else 'PENDING_SLIP'
        cur=c.execute('''INSERT INTO bookings(user_id,table_id,cue_id,start_at,end_at,status,table_fee_sat,cue_fee_sat,discount_sat,total_sat,credit_sat,due_sat,hold_expires_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)''',(uid,table_id,q['cues'][0]['cue_id'] if q['cues'] else None,dstr(q['start']),dstr(q['end']),status,q['table_fee_sat'],q['cue_fee_sat'],q['discount_sat'],q['total_sat'],credit_sat,due,dstr(now()+timedelta(minutes=30))))
        bid=cur.lastrowid
        if redeem_points:
            c.execute('UPDATE bookings SET redeemed_points=?,points_discount_sat=? WHERE booking_id=?',(redeem_points,q['points_discount_sat'],bid))
            c.execute("INSERT INTO points_wallet_ledger(user_id,booking_id,kind,delta_points) VALUES(?,?,'REDEEM',?)",(uid,bid,-redeem_points))
        for selected_cue in q['cues']:
            c.execute('INSERT INTO booking_cues(booking_id,cue_id,price_sat) VALUES(?,?,?)',(bid,selected_cue['cue_id'],selected_cue['price_sat']))
        if credit_sat:c.execute("INSERT INTO credit_ledger(user_id,booking_id,kind,delta_sat) VALUES(?,?,'USE',?)",(uid,bid,-credit_sat))
        return bid

def cancel_booking(uid,bid):
    with connect(True) as c:
        b=c.execute('SELECT * FROM bookings WHERE booking_id=? AND user_id=?',(bid,uid)).fetchone()
        if not b:raise RuleError('ไม่พบการจองนี้')
        if b['status'] not in ('PENDING_SLIP','REJECTED'):raise RuleError('ยกเลิกได้เฉพาะรายการที่ยังไม่ชำระหรือถูกปฏิเสธสลิป')
        c.execute("UPDATE bookings SET status='CANCELLED' WHERE booking_id=?",(bid,))
        if b['credit_sat']:c.execute("INSERT OR IGNORE INTO credit_ledger(user_id,booking_id,kind,delta_sat) VALUES(?,?,'RETURN',?)",(uid,bid,b['credit_sat']))
        if b['redeemed_points']:c.execute("INSERT OR IGNORE INTO points_wallet_ledger(user_id,booking_id,kind,delta_points) VALUES(?,?,'RETURN',?)",(uid,bid,b['redeemed_points']))

def admin_cancel_booking(admin_id,bid):
    """Cancel before check-in, credit exactly the verified payment + applied credit.

    An uploaded but unapproved slip is not proof of payment. The action is atomic:
    status, refund, points and audit either all commit or all roll back.
    """
    with connect(True) as c:
        staff=c.execute('SELECT role,is_active FROM staff_users WHERE staff_id=?',(admin_id,)).fetchone()
        if not staff or not staff['is_active'] or staff['role']!='ADMIN':
            raise RuleError('เฉพาะ ADMIN เท่านั้นที่ยกเลิกรายการและคืนเครดิตได้')
        b=c.execute('SELECT * FROM bookings WHERE booking_id=?',(bid,)).fetchone()
        if not b:raise RuleError('ไม่พบรายการจอง')
        if b['status'] not in ('PENDING_SLIP','UNDER_REVIEW','REJECTED','CONFIRMED'):
            raise RuleError('ยกเลิกได้เฉพาะรายการที่ยังไม่เช็กอินและยังไม่สิ้นสุด')
        paid=c.execute('SELECT COALESCE(SUM(amount_sat),0) FROM payment_slips WHERE booking_id=? AND status="APPROVED"',(bid,)).fetchone()[0]
        # Paid bookings have an approved slip, unless paid solely via Booking Credit.
        if b['status']=='CONFIRMED' and paid!=b['due_sat']:
            raise RuleError('ยอดที่อนุมัติไม่ตรงกับยอดชำระ กรุณาตรวจสอบก่อนยกเลิก')
        if b['status']!='CONFIRMED' and paid:
            raise RuleError('รายการมีสลิปอนุมัติแต่สถานะไม่ตรง กรุณาตรวจสอบก่อนยกเลิก')
        refund=int(b['credit_sat'])+int(paid)
        c.execute('UPDATE bookings SET status="CANCELLED" WHERE booking_id=?',(bid,))
        c.execute('UPDATE payment_slips SET status="REPLACED",review_note="ยกเลิกการจองโดย ADMIN" WHERE booking_id=? AND status="PENDING"',(bid,))
        if refund:
            # Credit RETURN is unique per booking, so simultaneous/repeated requests cannot double-refund.
            c.execute('INSERT INTO credit_ledger(user_id,booking_id,kind,delta_sat) VALUES(?, ?, "RETURN", ?)',(b['user_id'],bid,refund))
        if b['redeemed_points']:
            c.execute('INSERT OR IGNORE INTO points_wallet_ledger(user_id,booking_id,kind,delta_points) VALUES(?, ?, "RETURN", ?)',(b['user_id'],bid,b['redeemed_points']))
        # Awarded points cannot also remain after the charge is fully converted to credit.
        c.execute('UPDATE points_ledger SET points=0 WHERE booking_id=?',(bid,))
        c.execute('INSERT INTO audit_logs(staff_id,event,entity_type,entity_id,note) VALUES(?,"BOOKING_CANCEL_REFUND","BOOKING",?,?)',
                  (admin_id,bid,'Booking Credit refund sat='+str(refund)))
        return refund

def add_slip(uid,bid,filename,amount):
    with connect(True) as c:
        b=c.execute('SELECT * FROM bookings WHERE booking_id=? AND user_id=?',(bid,uid)).fetchone()
        if not b or b['status'] not in ('PENDING_SLIP','REJECTED','UNDER_REVIEW') or b['due_sat']<=0:raise RuleError('รายการนี้ไม่อยู่ในสถานะรอแนบสลิป')
        if b['status']=='PENDING_SLIP' and b['hold_expires_at']<=moment():raise RuleError('หมดเวลาเก็บการจองแล้ว กรุณาจองใหม่')
        # A new upload invalidates previously pending/rejected slips atomically;
        # administrators cannot approve a replaced file.
        c.execute("UPDATE payment_slips SET status='REPLACED' WHERE booking_id=? AND status IN ('PENDING','REJECTED')",(bid,))
        if int(amount)!=int(b['due_sat']):raise RuleError('ยอดสลิปไม่ตรงกับยอดที่ต้องชำระ')
        c.execute('INSERT INTO payment_slips(booking_id,user_id,file_name,amount_sat,uploaded_at) VALUES(?,?,?,?,?)',(bid,uid,filename,amount,moment()))
        c.execute("UPDATE bookings SET status='UNDER_REVIEW' WHERE booking_id=?",(bid,))

def review_slip(admin_id,slip_id,approve,note=''):
    with connect(True) as c:
        staff=c.execute('SELECT * FROM staff_users WHERE staff_id=? AND is_active=1',(admin_id,)).fetchone()
        if not staff or staff['role']!='ADMIN':raise RuleError('เฉพาะ ADMIN เท่านั้นที่ตรวจสลิปได้')
        slip=c.execute('SELECT * FROM payment_slips WHERE slip_id=?',(slip_id,)).fetchone()
        if not slip or slip['status']!='PENDING':raise RuleError('สลิปนี้ถูกตรวจสอบแล้ว')
        b=c.execute('SELECT * FROM bookings WHERE booking_id=?',(slip['booking_id'],)).fetchone()
        if not b or b['status']!='UNDER_REVIEW' or b['due_sat']!=slip['amount_sat']:raise RuleError('ยอดหรือสถานะการจองไม่ถูกต้อง')
        next_status='CONFIRMED' if approve else 'REJECTED'
        c.execute('UPDATE payment_slips SET status=?,reviewer_id=?,reviewed_at=?,review_note=? WHERE slip_id=?',('APPROVED' if approve else 'REJECTED',admin_id,moment(),note.strip()[:400],slip_id))
        c.execute('UPDATE bookings SET status=? WHERE booking_id=?',(next_status,b['booking_id']))
        if approve and b['due_sat']>=1000:
            c.execute('INSERT OR IGNORE INTO points_ledger(user_id,booking_id,points) VALUES(?,?,?)',(b['user_id'],b['booking_id'],b['due_sat']//1000))
        c.execute('INSERT INTO audit_logs(staff_id,event,entity_type,entity_id,note) VALUES(?,?,?,?,?)',(admin_id,'SLIP_APPROVED' if approve else 'SLIP_REJECTED','SLIP',slip_id,note.strip()[:400]))
        return b['booking_id']

def check_in(staff_id,bid):
    with connect(True) as c:
        b=c.execute('SELECT * FROM bookings WHERE booking_id=?',(bid,)).fetchone()
        if not b or b['status']!='CONFIRMED':raise RuleError('ต้องได้รับการยืนยันชำระเงินก่อนเช็กอิน')
        start=parse_time(b['start_at']); current=now()
        if current<start or current>start+timedelta(minutes=15):raise RuleError('เช็กอินได้ตั้งแต่เวลาเริ่มจนถึง 15 นาทีหลังเริ่มเท่านั้น')
        # Time extension on late check-in requires no later overlap.
        new_end=current+(parse_time(b['end_at'])-start)
        hit=c.execute("SELECT 1 FROM bookings WHERE table_id=? AND booking_id<>? AND status IN ('CONFIRMED','CHECKED_IN','UNDER_REVIEW') AND start_at<? AND end_at>? LIMIT 1",(b['table_id'],bid,dstr(new_end),dstr(current))).fetchone()
        if hit:raise RuleError('เวลาเล่นจะชนการจองถัดไป กรุณาย้ายโต๊ะหรือแจ้งพนักงาน')
        c.execute("UPDATE bookings SET status='CHECKED_IN', actual_check_in_at=?,end_at=? WHERE booking_id=?",(dstr(current),dstr(new_end),bid))

def check_out(staff_id,bid):
    with connect(True) as c:
        b=c.execute('SELECT * FROM bookings WHERE booking_id=?',(bid,)).fetchone()
        if not b or b['status']!='CHECKED_IN':raise RuleError('รายการนี้ไม่ได้อยู่ระหว่างใช้โต๊ะ')
        c.execute("UPDATE bookings SET status='COMPLETED' WHERE booking_id=?",(bid,))

def sweep():
    """Update expired holds and settled, non-checked-in bookings; idempotent."""
    with connect(True) as c:
        expired=c.execute("SELECT booking_id,user_id,credit_sat FROM bookings WHERE status='PENDING_SLIP' AND hold_expires_at<=?",(moment(),)).fetchall()
        for b in expired:
            c.execute("UPDATE bookings SET status='CANCELLED' WHERE booking_id=?",(b['booking_id'],))
            if b['credit_sat']:
                c.execute("INSERT OR IGNORE INTO credit_ledger(user_id,booking_id,kind,delta_sat) VALUES(?,?,'RETURN',?)",(b['user_id'],b['booking_id'],b['credit_sat']))
            points=c.execute('SELECT redeemed_points FROM bookings WHERE booking_id=?',(b['booking_id'],)).fetchone()[0]
            if points:c.execute("INSERT OR IGNORE INTO points_wallet_ledger(user_id,booking_id,kind,delta_points) VALUES(?,?,'RETURN',?)",(b['user_id'],b['booking_id'],points))
        due=c.execute("SELECT booking_id,user_id,total_sat FROM bookings WHERE status='CONFIRMED' AND datetime(start_at,'+15 minutes')<?",(moment(),)).fetchall()
        for b in due:
            c.execute("UPDATE bookings SET status='NO_SHOW' WHERE booking_id=? AND status='CONFIRMED'",(b['booking_id'],))
            c.execute("INSERT OR IGNORE INTO credit_ledger(user_id,booking_id,kind,delta_sat) VALUES(?,?,'NO_SHOW',?)",(b['user_id'],b['booking_id'],b['total_sat']))

def request_reset(email,send=None):
    """Do not expose whether a customer exists. Never return code in response."""
    email=email.strip().lower()
    with connect() as c:
        row=c.execute('SELECT user_id FROM users WHERE lower(email)=? AND is_active=1',(email,)).fetchone()
        prev=c.execute('SELECT requested_at FROM password_reset WHERE user_id=?',(row['user_id'],)).fetchone() if row else None
    if not row or (prev and parse_time(prev['requested_at'])>now()-timedelta(seconds=60)):return
    code=f'{secrets.randbelow(100000000):08d}'
    # Only persist reset after SMTP delivery succeeded.
    sender=send or send_mail
    sender(email,code)
    salt=secrets.token_bytes(16)
    digest=hashlib.pbkdf2_hmac('sha256',code.encode(),salt,260000)
    with connect(True) as c:
        c.execute('''INSERT INTO password_reset(user_id,hash,expires_at,requested_at,attempts) VALUES(?,?,?,?,0)
             ON CONFLICT(user_id) DO UPDATE SET hash=excluded.hash,expires_at=excluded.expires_at,requested_at=excluded.requested_at,attempts=0''',
             (row['user_id'],salt.hex()+'$'+digest.hex(),dstr(now()+timedelta(minutes=10)),moment()))

# Public identifiers, NOT secrets. Set after deploying your Apps Script and Turnstile widget.
OTP_RELAY_URL = 'https://script.google.com/macros/s/AKfycbzlxqdTNQzIlwA9pUwu_K2TwQ5XYYqRoVSgJ76ERmxDSKluPa6hs7TXEtSyLNKLWQLkNg/exec'
TURNSTILE_SITEKEY = '0x4AAAAAAFqixMTMFSq4YmK2'


def use_central_otp():
    # Prefer the configured central relay even when old SMTP values exist.
    # Fall back to the original local SMTP sender when the relay is not set.
    relay = os.environ.get('RNGD_OTP_RELAY_URL', OTP_RELAY_URL).strip()
    sitekey = os.environ.get('RNGD_TURNSTILE_SITEKEY', TURNSTILE_SITEKEY).strip()
    if relay.startswith('https://script.google.com/macros/s/') and relay.endswith('/exec') and sitekey and not sitekey.startswith('PASTE_'):
        return True
    keys=('RNGD_SMTP_HOST','RNGD_SMTP_PORT','RNGD_SMTP_USERNAME','RNGD_SMTP_PASSWORD','RNGD_SMTP_FROM')
    return not all(os.environ.get(k) for k in keys)


def public_turnstile_sitekey():
    return os.environ.get('RNGD_TURNSTILE_SITEKEY',TURNSTILE_SITEKEY).strip()


def send_mail(destination,code,captcha_token=''):
    keys=('RNGD_SMTP_HOST','RNGD_SMTP_PORT','RNGD_SMTP_USERNAME','RNGD_SMTP_PASSWORD','RNGD_SMTP_FROM')
    if not use_central_otp() and all(os.environ.get(k) for k in keys):
        # Original SMTP mechanism, used only when central relay is not configured.
        port=int(os.environ['RNGD_SMTP_PORT']);host=os.environ['RNGD_SMTP_HOST']
        if port not in (465,587):raise RuleError('SMTP รองรับ TLS พอร์ต 465 หรือ 587')
        msg=EmailMessage();msg['From']=os.environ['RNGD_SMTP_FROM'];msg['To']=destination;msg['Subject']='Roo Nai Gor Dai - Password reset'
        msg.set_content(f'รหัสยืนยันของคุณคือ {code}\nใช้ได้ 10 นาที และใช้ได้หนึ่งครั้ง')
        context=ssl.create_default_context()
        try:
            if port==465:
                with smtplib.SMTP_SSL(host,port,timeout=12,context=context) as smtp:smtp.login(os.environ['RNGD_SMTP_USERNAME'],os.environ['RNGD_SMTP_PASSWORD']);smtp.send_message(msg)
            else:
                with smtplib.SMTP(host,port,timeout=12) as smtp:smtp.starttls(context=context);smtp.login(os.environ['RNGD_SMTP_USERNAME'],os.environ['RNGD_SMTP_PASSWORD']);smtp.send_message(msg)
        except (OSError,smtplib.SMTPException):raise RuleError('ส่งอีเมลไม่สำเร็จ โปรดตรวจการตั้งค่า SMTP และเครือข่าย')
        return

    # Public download: the relay verifies a Turnstile token server-side.
    url=os.environ.get('RNGD_OTP_RELAY_URL',OTP_RELAY_URL).strip()
    if not (url.startswith('https://script.google.com/macros/s/') and url.endswith('/exec')):
        raise RuleError('ยังไม่ได้ตั้งค่าบริการส่ง OTP ส่วนกลาง')
    sitekey=public_turnstile_sitekey()
    if not sitekey or sitekey.startswith('PASTE_'):
        raise RuleError('ยังไม่ได้ตั้งค่า Turnstile เพื่อส่ง OTP อย่างปลอดภัย')
    if not captcha_token or len(captcha_token)>2048:
        raise RuleError('กรุณายืนยันการป้องกันสแปมก่อนขอรหัส OTP')

    import json
    from urllib.request import Request,urlopen
    from urllib.error import HTTPError,URLError
    payload=json.dumps({'email':destination,'code':code,'captcha_token':captcha_token}).encode('utf-8')
    request=Request(url,data=payload,headers={'Content-Type':'application/json; charset=utf-8'},method='POST')
    try:
        with urlopen(request,timeout=18) as response:
            result=json.loads(response.read(8192).decode('utf-8'))
    except (HTTPError,URLError,TimeoutError,ValueError,UnicodeDecodeError,OSError) as exc:
        raise RuleError('เชื่อมต่อบริการส่ง OTP ไม่สำเร็จ โปรดลองใหม่') from exc
    if result.get('ok') is not True:
        raise RuleError('ไม่สามารถส่ง OTP ได้ โปรดตรวจการยืนยันผู้ใช้หรือรอแล้วลองอีกครั้ง')

def reset_password(email,code,password,confirmation):
    if not password_ok(password):raise RuleError('รหัสผ่านใหม่ต้องยาว 6–60 ตัวอักษร')
    if password!=confirmation:raise RuleError('รหัสผ่านใหม่สองช่องไม่ตรงกัน')
    with connect(True) as c:
        row=c.execute('SELECT u.user_id,r.* FROM users u JOIN password_reset r ON r.user_id=u.user_id WHERE lower(u.email)=lower(?) AND u.is_active=1',(email.strip(),)).fetchone()
        if not row or row['expires_at']<moment() or row['attempts']>=5:raise RuleError('รหัสยืนยันไม่ถูกต้องหรือหมดอายุ')
        try:
            salt,digest=row['hash'].split('$'); valid=hmac.compare_digest(hashlib.pbkdf2_hmac('sha256',code.encode(),bytes.fromhex(salt),260000),bytes.fromhex(digest))
        except (ValueError,TypeError):valid=False
        if not valid:
            c.execute('UPDATE password_reset SET attempts=attempts+1 WHERE user_id=?',(row['user_id'],))
            # Commit failed-attempt counter, then surface error outside transaction.
            error=True
        else:
            error=False
            c.execute('UPDATE users SET password_hash=? WHERE user_id=?',(hash_password(password),row['user_id']))
            c.execute('DELETE FROM password_reset WHERE user_id=?',(row['user_id'],))
            c.execute('DELETE FROM auth_sessions WHERE user_id=?',(row['user_id'],))
    if error:raise RuleError('รหัสยืนยันไม่ถูกต้อง กรุณาลองอีกครั้ง')

def verify_reset_step(email,code,session_token):
    """Validate emailed OTP before rendering the new password form."""
    email=email.strip().lower()
    wrong=False
    with connect(True) as c:
        row=c.execute("SELECT u.user_id,r.* FROM users u JOIN password_reset r ON r.user_id=u.user_id WHERE lower(u.email)=? AND u.is_active=1",(email,)).fetchone()
        if not row or row['expires_at']<=moment() or row['attempts']>=5:raise RuleError('รหัสยืนยันไม่ถูกต้องหรือหมดอายุ')
        try:
            salt,digest=row['hash'].split('$')
            valid=hmac.compare_digest(hashlib.pbkdf2_hmac('sha256',code.encode(),bytes.fromhex(salt),260000),bytes.fromhex(digest))
        except (ValueError,TypeError):valid=False
        if not valid:
            c.execute('UPDATE password_reset SET attempts=attempts+1 WHERE user_id=?',(row['user_id'],))
            wrong=True
        else:
            c.execute('DELETE FROM password_reset WHERE user_id=?',(row['user_id'],))
            c.execute('INSERT OR REPLACE INTO reset_grants(session_hash,user_id,expires_at) VALUES(?,?,?)',
                (hashlib.sha256(session_token.encode()).hexdigest(),row['user_id'],dstr(now()+timedelta(minutes=10))))
    if wrong:raise RuleError('รหัสยืนยันไม่ถูกต้อง กรุณาลองอีกครั้ง')

def has_reset_grant(session_token):
    with connect() as c:
        return c.execute('SELECT 1 FROM reset_grants WHERE session_hash=? AND expires_at>?',
            (hashlib.sha256(session_token.encode()).hexdigest(),moment())).fetchone() is not None

def finish_verified_reset(session_token,password,confirmation):
    if not password_ok(password):raise RuleError('รหัสผ่านใหม่ต้องยาว 6–60 ตัวอักษร')
    if password!=confirmation:raise RuleError('รหัสผ่านสองช่องไม่ตรงกัน')
    with connect(True) as c:
        token_hash=hashlib.sha256(session_token.encode()).hexdigest()
        row=c.execute('SELECT user_id FROM reset_grants WHERE session_hash=? AND expires_at>?',(token_hash,moment())).fetchone()
        if not row:raise RuleError('กรุณากรอกรหัสยืนยันจากอีเมลก่อนตั้งรหัสใหม่')
        c.execute('UPDATE users SET password_hash=? WHERE user_id=?',(hash_password(password),row['user_id']))
        c.execute('DELETE FROM reset_grants WHERE user_id=?',(row['user_id'],))
        c.execute('DELETE FROM password_reset WHERE user_id=?',(row['user_id'],))
        c.execute('DELETE FROM auth_sessions WHERE user_id=?',(row['user_id'],))

def delete_user(admin_id,user_id):
    with connect(True) as c:
        staff=c.execute('SELECT role FROM staff_users WHERE staff_id=? AND is_active=1',(admin_id,)).fetchone()
        if not staff or staff['role']!='ADMIN':raise RuleError('ไม่มีสิทธิ์ลบผู้ใช้')
        if not c.execute('SELECT 1 FROM users WHERE user_id=?',(user_id,)).fetchone():raise RuleError('ไม่พบผู้ใช้')
        # Deletion without financial records can be physical; otherwise scrub PII
        # and keep a non-login bookkeeping placeholder for referential integrity.
        has_booking=c.execute('SELECT 1 FROM bookings WHERE user_id=? LIMIT 1',(user_id,)).fetchone()
        if has_booking:
            c.execute('DELETE FROM auth_sessions WHERE user_id=?',(user_id,))
            c.execute('DELETE FROM password_reset WHERE user_id=?',(user_id,))
            c.execute('DELETE FROM reset_grants WHERE user_id=?',(user_id,))
            c.execute("UPDATE users SET username=?,email=?,phone=?,full_name='ผู้ใช้ที่ลบแล้ว',password_hash='',is_active=0 WHERE user_id=?",(f'deleted_{user_id}',f'deleted_{user_id}@invalid.local',f'X{user_id}',user_id))
        else:
            c.execute('DELETE FROM auth_sessions WHERE user_id=?',(user_id,))
            c.execute('DELETE FROM password_reset WHERE user_id=?',(user_id,))
            c.execute('DELETE FROM reset_grants WHERE user_id=?',(user_id,))
            c.execute('DELETE FROM users WHERE user_id=?',(user_id,))
        c.execute("INSERT INTO audit_logs(staff_id,event,entity_type,entity_id,note) VALUES(?,'USER_DELETED','USER',?,'privacy erase')",(admin_id,user_id))

STATUS={'PENDING_SLIP':'รอแนบสลิป','UNDER_REVIEW':'รอแอดมินตรวจสลิป','REJECTED':'สลิปไม่ผ่าน','CONFIRMED':'ยืนยันการจองแล้ว','CHECKED_IN':'กำลังเล่น','COMPLETED':'เสร็จสิ้น','NO_SHOW':'ไม่มาเช็กอิน','CANCELLED':'ยกเลิก'}

def money(sat):return f'฿{sat/100:,.2f}'


def create_admin(actor_id, username, password, confirmation):
    """Create a new ADMIN using the existing staff table, with role authorization."""
    username=username.strip()
    if not (3 <= len(username) <= 60) or not re.fullmatch(r'[A-Za-z0-9_.-]+',username):
        raise RuleError('ชื่อผู้ใช้ต้องยาว 3–60 ตัว และใช้ได้เฉพาะตัวอักษรอังกฤษ ตัวเลข _ . -')
    if not password_ok(password):raise RuleError('รหัสผ่านต้องยาว 6–60 ตัวอักษร')
    if password!=confirmation:raise RuleError('รหัสผ่านทั้งสองช่องไม่ตรงกัน')
    with connect(True) as c:
        actor=c.execute('SELECT role,is_active FROM staff_users WHERE staff_id=?',(actor_id,)).fetchone()
        if not actor or actor['role']!='ADMIN' or not actor['is_active']:
            raise RuleError('เฉพาะ ADMIN ที่ใช้งานอยู่เท่านั้นที่เพิ่มบัญชีได้')
        if c.execute('SELECT 1 FROM staff_users WHERE lower(username)=lower(?)',(username,)).fetchone():
            raise RuleError('ชื่อผู้ใช้นี้มีอยู่แล้ว')
        try:
            result=c.execute("INSERT INTO staff_users(username,password_hash,role,is_active) VALUES(?,?,'ADMIN',1)",(username,hash_password(password)))
        except sqlite3.IntegrityError:raise RuleError('ชื่อผู้ใช้นี้มีอยู่แล้ว')
        c.execute("INSERT INTO audit_logs(staff_id,event,entity_type,entity_id,note) VALUES(?,'ADMIN_CREATED','STAFF',?,?)",(actor_id,result.lastrowid,username))
        return result.lastrowid
