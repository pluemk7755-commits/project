"""Python 3 standard-library HTTP server for Roo Nai Gor Dai web v1.
Local/LAN preview only. Public deployment requires HTTPS reverse proxy and
server hardening; no automatic payment gateway is included.
"""
from __future__ import annotations
import argparse, hashlib, html, io, json, os, re, secrets, sqlite3, threading, time
from datetime import date, timedelta
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from http.cookies import SimpleCookie
from urllib.parse import urlparse,parse_qs,quote,urlencode
from email.parser import BytesParser
from email import policy
from PIL import Image, ImageOps, UnidentifiedImageError
from . import core as db

BASE=db.BASE; STAT=BASE/'static'
E=lambda v:html.escape(str(v or ''),quote=True)

def query(url):return {k:v[-1] for k,v in parse_qs(urlparse(url).query).items()}
def thai_status(row):return db.STATUS.get(row['status'], row['status'])
def dt_html(val):return E(val.replace(' ','T')[:16])
def money(sat):return db.money(sat)

def img(name):return '/static/images/'+quote(name)

# Shared catalog images: the booking picker uses the exact same image as the cue catalog.
CUE_CATALOG_IMAGES = {
    ('Predator', 'REVO 12.4'): 'cue_revo_catalog_full.webp',
    ('Cuetec', 'Cynergy 12.5'): 'cue_cynergy_catalog_full.webp',
    ('Mezz', 'EC9'): 'cue_mezz_catalog_full.webp',
    ('Peradon', 'Classic'): 'cue_peradon_catalog_full.webp',
    ('Predator', 'Aspire'): 'cue_aspire_catalog_full.webp',
    ("O'Min", "O'Millenium 3/4"): 'cue_omin_millenium_distinct.webp',
    ("O'Min", 'Professional 3/4'): 'cue_omin_professional_distinct.webp',
    ("O'Min", 'Perfect 3/4'): 'cue_omin_perfect_distinct.webp',
    ('PowerGlide', 'Asas 3/4'): 'cue_powerglide_asas_distinct.webp',
    ('Peradon', 'Crown 3/4'): 'cue_peradon_crown_distinct.webp',
}

def cue_catalog_image(cue):
    return img(CUE_CATALOG_IMAGES.get((str(cue['brand']), str(cue['model'])), cue['img']))

def payee_settings():
    """Account supplied by shop owner, never silently switch to demo bank info."""
    local=db.DATA/'payee_config.json'
    stored={}
    if local.is_file():
        try:stored=json.loads(local.read_text(encoding='utf-8'))
        except (ValueError,OSError):pass
    result={
      'shop': stored.get('shop','ROO NAI GOR DAI'),
      'bank':os.environ.get('RNGD_BANK_NAME') or stored.get('bank',''),
      'name':os.environ.get('RNGD_PAYEE_NAME') or stored.get('name',''),
      'account':os.environ.get('RNGD_BANK_ACCOUNT') or stored.get('account',''),
      'configured':False,
    }
    result['configured']=all(result[k] for k in ('bank','name','account')) and (db.DATA/'payee_qr.png').is_file()
    return result

def alert(message,level='info'):
    return f'<div class="alert {E(level)}" role="status">{E(message)}</div>'

# Admin list queries are filtered in SQLite *before* LIMIT/OFFSET, so older
# bookings remain searchable and status selection uses the correct entity.
ADMIN_PAGE_SIZE = 100

def admin_search_rows(section, args, page_size=ADMIN_PAGE_SIZE):
    if section not in ('booking','payments'):
        raise ValueError('Invalid admin search section')
    is_payment=section=='payments'
    mode=args.get('search_by','booking_id')
    allowed_modes={'booking_id','table','customer'} | ({'slip_id'} if is_payment else set())
    if mode not in allowed_modes: mode='booking_id'
    status=args.get('status','ALL').upper()
    if not is_payment and status=='PENDING': status='PENDING_SLIP'  # legacy URL
    allowed_states=({'ALL','PENDING','APPROVED','REJECTED','REPLACED'} if is_payment
                    else {'ALL','PENDING_SLIP','UNDER_REVIEW','REJECTED','CONFIRMED',
                          'CHECKED_IN','COMPLETED','NO_SHOW','CANCELLED'})
    if status not in allowed_states: status='ALL'
    q=args.get('q','').strip()[:80]
    base=('FROM payment_slips p JOIN bookings b ON b.booking_id=p.booking_id '
          'JOIN users u ON u.user_id=b.user_id JOIN club_tables t ON t.table_id=b.table_id' if is_payment
          else 'FROM bookings b JOIN users u ON u.user_id=b.user_id JOIN club_tables t ON t.table_id=b.table_id')
    clauses=[]; params=[]
    if status!='ALL':
        clauses.append(('p' if is_payment else 'b')+'.status=?');params.append(status)
    with db.connect() as c:
        if q:
            if mode in ('booking_id','slip_id'):
                number=q.lstrip('#')
                if number.isascii() and number.isdecimal():
                    clauses.append(('b.booking_id' if mode=='booking_id' else 'p.slip_id')+'=?')
                    params.append(int(number))
                else:clauses.append('0=1')
            elif mode=='customer':
                # Escape SQL LIKE wildcards: search treats % and _ literally.
                escaped=q.replace('\\','\\\\').replace('%','\\%').replace('_','\\_')
                clauses.append("u.full_name LIKE ? ESCAPE '\\'")
                params.append('%'+escaped+'%')
            else:  # table code; a numeric lookup matches the table suffix exactly
                if q.isascii() and q.isdecimal():
                    n=int(q)
                    table_ids=[]
                    for table in c.execute('SELECT table_id,table_code FROM club_tables'):
                        suffix=re.search(r'(\d+)$',table['table_code'])
                        if suffix and int(suffix.group(1))==n:
                            table_ids.append(table['table_id'])
                    if table_ids:
                        clauses.append('b.table_id IN ('+','.join('?' for _ in table_ids)+')')
                        params.extend(table_ids)
                    else:clauses.append('0=1')
                else:
                    escaped=q.replace('\\','\\\\').replace('%','\\%').replace('_','\\_')
                    clauses.append("t.table_code LIKE ? ESCAPE '\\'")
                    params.append('%'+escaped+'%')
        where=(' WHERE '+' AND '.join(clauses)) if clauses else ''
        total=c.execute('SELECT COUNT(*) '+base+where,params).fetchone()[0]
        try:requested_page=int(args.get('page','1'))
        except (ValueError,TypeError):requested_page=1
        last_page=max(1,(total+page_size-1)//page_size)
        page=min(max(1,requested_page),last_page)
        select=('p.*,b.due_sat,b.start_at,t.table_code,u.full_name' if is_payment
                else 'b.*,u.full_name,t.table_code')
        order='p.slip_id' if is_payment else 'b.booking_id'
        rows=c.execute(f'SELECT {select} {base}{where} ORDER BY {order} DESC LIMIT ? OFFSET ?',
                       (*params,page_size,(page-1)*page_size)).fetchall()
    return rows,total,page,mode,status

class AppHandler(BaseHTTPRequestHandler):
    server_version='RNGD-Web/0.1'
    def log_message(self,fmt,*args):
        # Never log passwords, form data, or credentials.
        if os.environ.get('RNGD_QUIET')!='1':super().log_message(fmt,*args)
    def _ensure_session(self):
        cookies=SimpleCookie()
        try:cookies.load(self.headers.get('Cookie',''))
        except Exception:pass
        self.is_staff_route=(urlparse(self.path).path.startswith(('/admin','/staff/','/api/admin/')))
        self.cookie_name='rngd_staff_session' if self.is_staff_route else 'rngd_session'
        self.token=cookies[self.cookie_name].value if self.cookie_name in cookies else ''
        self.session=db.get_session(self.token)
        self.cookie_new=False
        if not self.session:
            self.token,_=db.create_session()
            self.session=db.get_session(self.token)
            self.cookie_new=True
    def _cookie_header(self):
        # Static assets are served before _ensure_session() is called.
        # Never require a user session to deliver CSS, JavaScript or images.
        if not getattr(self, 'cookie_new', False):return None
        secure='; Secure' if os.environ.get('RNGD_COOKIE_SECURE')=='1' else ''
        return f'{self.cookie_name}={self.token}; HttpOnly; SameSite=Lax; Path=/; Max-Age=21600{secure}'
    def send(self, body,code=200,kind='text/html; charset=utf-8',headers=None):
        if isinstance(body,str):body=body.encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type',kind);self.send_header('Content-Length',str(len(body)))
        self.send_header('X-Content-Type-Options','nosniff')
        self.send_header('Referrer-Policy','same-origin')
        self.send_header('X-Frame-Options','DENY')
        csp="default-src 'self'; img-src 'self' data:; style-src 'self' https://fonts.googleapis.com; font-src 'self' https://fonts.gstatic.com; script-src 'self'; base-uri 'self'; form-action 'self'"
        if urlparse(self.path).path=='/forgot' and db.use_central_otp():
            csp=("default-src 'self'; img-src 'self' data:; style-src 'self' https://fonts.googleapis.com "
                 "https://challenges.cloudflare.com; font-src 'self' https://fonts.gstatic.com; "
                 "script-src 'self' https://challenges.cloudflare.com; "
                 "frame-src https://challenges.cloudflare.com; "
                 "connect-src 'self' https://challenges.cloudflare.com; base-uri 'self'; form-action 'self'")
        self.send_header('Content-Security-Policy',csp)
        cookie=self._cookie_header()
        if cookie:self.send_header('Set-Cookie',cookie)
        for k,v in (headers or {}).items():self.send_header(k,v)
        self.end_headers();self.wfile.write(body)
    def go(self,location):self.send(b'',303,headers={'Location':location})
    def csrf(self):return f'<input type="hidden" name="csrf" value="{E(self.session["csrf"])}">'
    def link(self,name,path):return f'<a href="{path}">{name}</a>'
    def layout(self,body,title='Roo Nai Gor Dai',active='home',message=''):
        logged=self.session and (self.session['user_id'] or self.session['staff_id'])
        staff=bool(self.session and self.session['staff_id'])
        name=(self.session.get('staff_name') if staff else self.session.get('full_name')) if logged else None
        if staff:
            items=[('overview','ภาพรวม','/admin'),('booking','การจอง / เช็กอิน','/admin?view=booking'),('payments','ตรวจสลิป','/admin?view=payments'),('revenue','รายได้','/admin?view=revenue'),('tables','จัดการโต๊ะ','/admin?view=tables'),('users','จัดการผู้ใช้','/admin/users'),('admins','จัดการ ADMIN','/admin/admins')]
        else:
            items=[('home','หน้าหลัก','/'),('tables','จองโต๊ะ','/tables'),('cues','ไม้คิวพรีเมียม','/cues'),('my','รายการของฉัน','/my')]
        nav=''.join(f'<a class="navitem {"current" if active==item else ""}" href="{url}">{label}</a>' for item,label,url in items)
        if logged:
            user_controls=f'<a class="profile-name" href="{"/admin" if staff else "/profile"}" title="โปรไฟล์">{E(name)}</a><form method="post" action="{'/staff/logout' if staff else '/logout'}">{self.csrf()}<button class="link-button">ออกจากระบบ</button></form>'
        else:user_controls='<a class="pill secondary" href="/login">เข้าสู่ระบบ</a><a class="pill primary" href="/register">สมัครสมาชิก</a>'
        return f'''<!DOCTYPE html><html lang="th"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{E(title)} | Roo Nai Gor Dai</title><link rel="icon" href="{img('snooker_red_ball.png')}"><link rel="stylesheet" href="/static/style.css"><script defer src="/static/app.js"></script></head><body>
           <header class="topbar"><a class="brand" href="{"/admin" if staff else "/"}"><img src="{img('snooker_red_ball.png')}" alt="ลูกสนุกเกอร์สีแดง"><span>ROO NAI GOR DAI<small>SNOOKER CLUB & BOOKINGS</small></span></a><button class="menu-toggle" type="button" aria-label="เปิดเมนู" aria-expanded="false">☰</button><nav class="navigation">{nav}</nav><div class="user-tools">{user_controls}</div></header>
           <main class="page">{alert(message,'success') if message else ''}{body}</main><footer>ROO NAI GOR DAI · เว็บไซต์ระบบจองโต๊ะ · การตรวจสลิปโดยผู้ดูแลร้าน</footer></body></html>'''
    def page(self,body,title='',active='home',message='',code=200):self.send(self.layout(body,title,active,message),code)
    def user_required(self):
        if not self.session['user_id']:raise db.RuleError('กรุณาเข้าสู่ระบบสมาชิกก่อน')
        return self.session['user_id']
    def staff_required(self,admin=False):
        # Keep the optional argument for existing route compatibility. STAFF
        # and ADMIN are now one unified administrator role with equal access.
        if not self.session['staff_id']:raise db.RuleError('กรุณาเข้าสู่ระบบผู้ดูแลระบบ')
        return self.session['staff_id']
    def do_GET(self):
        try:
            path=urlparse(self.path).path
            if path.startswith('/static/'):
                p=(STAT/path.removeprefix('/static/')).resolve()
                if not p.is_relative_to(STAT) or not p.is_file():return self.send('ไม่พบไฟล์',404)
                mime= 'text/css' if p.suffix=='.css' else 'text/javascript' if p.suffix=='.js' else 'image/png' if p.suffix=='.png' else 'image/webp' if p.suffix=='.webp' else 'image/jpeg'
                return self.send(p.read_bytes(),kind=mime)
            self._ensure_session();args=query(self.path)
            if path=='/':return self.home()
            if path=='/tables':return self.tables(args)
            if path=='/cues':return self.cues()
            if path=='/register':return self.register_page()
            if path=='/login':return self.login_page()
            if path=='/staff/login':return self.login_page(staff=True,args=args)
            if path=='/forgot':return self.forgot_page()
            if path=='/reset':return self.reset_page()
            if path=='/verify':return self.verify_page(args)
            if path=='/my':return self.my_bookings()
            if path=='/profile':return self.profile_page()
            if path=='/book':return self.book_page(args)
            if path=='/slip':return self.slip_page(args)
            if path=='/receipt':return self.receipt_page(args)
            if path=='/api/quote':return self.quote_json(args)
            if path=='/admin':return self.admin_page(args)
            if path=='/admin/users':return self.admin_users(args)
            if path=='/admin/admins':return self.admin_admins(args)
            if path=='/admin/slip-file':return self.slip_file(args)
            if path=='/admin/slip-view':return self.slip_view(args)
            if path=='/payee-qr':return self.payee_qr()
            if path=='/api/admin/status':return self.admin_status_json()
            self.page(alert('ไม่พบหน้านี้','error'),'ไม่พบหน้า',code=404)
        except db.RuleError as err:self.page(alert(str(err),'error'), 'ตรวจสอบข้อมูล',code=403)
        except Exception:
            self.page(alert('มีข้อผิดพลาดในการทำงาน กรุณาติดต่อผู้ดูแลระบบ','error'),'ระบบขัดข้อง',code=500)
            if os.environ.get('RNGD_DEBUG')=='1':import traceback;traceback.print_exc()
    def do_POST(self):
        try:
            self._ensure_session();fields,files=self.form()
            if not secrets.compare_digest(fields.get('csrf',''),self.session['csrf']):raise db.RuleError('เซสชันหมดอายุ กรุณาโหลดหน้าใหม่แล้วลองอีกครั้ง')
            path=urlparse(self.path).path
            handler={
                '/register':self.post_register,'/login':self.post_login,'/staff/login':self.post_staff_login,
                '/logout':self.post_logout,'/staff/logout':self.post_logout,'/profile':self.post_profile,'/forgot':self.post_forgot,'/verify':self.post_verify,'/reset':self.post_reset,
                '/book':self.post_book,'/slip':self.post_slip,'/cancel':self.post_cancel,
                '/admin/review':self.post_review,'/admin/cancel-booking':self.post_admin_cancel_booking,'/admin/delete-user':self.post_delete_user,
                '/admin/checkin':self.post_checkin,'/admin/checkout':self.post_checkout,
                '/admin/table-update':self.post_table_update,
                '/admin/create-admin':self.post_create_admin,
            }.get(path)
            if not handler:return self.page(alert('ไม่พบคำสั่งที่ร้องขอ','error'),code=404)
            handler(fields,files)
        except db.RuleError as err:
            # Failed administrator sign-in returns to the administrator login form,
            # preserving the error message and allowing another attempt.
            route=urlparse(self.path).path
            if route=='/staff/login':
                self.go('/staff/login?error=1')
            elif route=='/admin/create-admin':
                self.go('/admin/admins?error='+quote(str(err)))
            elif route in ('/admin/checkin','/admin/checkout','/admin/table-update','/admin/review','/admin/cancel-booking'):
                view='booking' if route in ('/admin/checkin','/admin/checkout','/admin/cancel-booking') else 'tables' if route=='/admin/table-update' else 'payments'
                self.go('/admin?view='+view+'&error='+quote(str(err)))
            else:
                self.page(alert(str(err),'error')+'<p><a href="/tables">กลับไปจองโต๊ะ</a></p>','ตรวจสอบข้อมูล',code=400)
        except Exception:
            self.page(alert('การทำงานไม่สำเร็จ กรุณาติดต่อผู้ดูแลระบบ','error'),code=500)
            if os.environ.get('RNGD_DEBUG')=='1':import traceback;traceback.print_exc()
    def form(self):
        length=int(self.headers.get('Content-Length','0'))
        if length>6*1024*1024:raise db.RuleError('ไฟล์ใหญ่เกิน 6 MB')
        body=self.rfile.read(length);ctype=self.headers.get('Content-Type','')
        if ctype.startswith('multipart/form-data'):
            root=BytesParser(policy=policy.default).parsebytes(b'MIME-Version: 1.0\r\nContent-Type: '+ctype.encode()+b'\r\n\r\n'+body)
            fields={};files={}
            for part in root.iter_parts():
                disp=part.get('Content-Disposition','')
                key=re.search(r'name="([^"]+)"',disp)
                if not key:continue
                name=key.group(1);value=part.get_payload(decode=True)
                if part.get_filename() is not None:files[name]=(part.get_filename(),value)
                else:fields[name]=value.decode('utf-8','replace')
            return fields,files
        return ({k:(','.join(v) if k=='cue_ids' else v[-1]) for k,v in parse_qs(body.decode('utf-8','replace')).items()}, {})
    def home(self):
        current=db.now()
        with db.connect() as c:
            tables=c.execute('SELECT * FROM club_tables WHERE status="OPEN" ORDER BY table_id').fetchall()
            busy=c.execute("SELECT DISTINCT table_id FROM bookings WHERE start_at<=? AND end_at>? AND status IN ('CONFIRMED','CHECKED_IN','UNDER_REVIEW')",(db.moment(),db.moment())).fetchall()
            used={r['table_id'] for r in busy}
        free=max(0,len(tables)-len(used))
        rates={r['table_type']:r['hourly_rate_sat'] for r in tables}
        banner=f'''<section class="hero"><div class="hero-text"><span class="eyebrow">ระบบจองโต๊ะออนไลน์</span><h1>ROO NAI GOR DAI<br><em>จองโต๊ะสนุกเกอร์</em></h1><p>เลือกโต๊ะและเวลา จองออนไลน์ แล้วแนบสลิปให้ร้านตรวจสอบ</p><div class="actions"><a class="pill primary large" href="/tables">ค้นหาโต๊ะว่าง <span>↗</span></a><a class="pill glass large" href="/cues">สำรวจไม้คิวพรีเมียม</a></div><div class="hero-stats"><div><b class="available-count">{free}</b><small>จำนวนโต๊ะว่างตอนนี้</small></div><div><b>10:00–02:00</b><small>เวลาเปิด–ปิด</small></div><div><b>15 นาที</b><small>เวลาเช็กอิน</small></div></div></div><div class="hero-photo"><span class="photo-label">ROO NAI GOR DAI · SNOOKER CLUB</span></div></section>'''
        cards=''.join(f'''<a class="visual-card visual-card-refresh" href="/tables?type={kind}" aria-label="เลือกโต๊ะ {E(label)} ราคาเริ่มต้น {money(rates.get(kind,0))} ต่อชั่วโมง"><img loading="lazy" src="{img(pic)}" alt="บรรยากาศโต๊ะ {E(label)}"><div class="visual-card-content"><div class="visual-card-title"><h3>{E(label)}</h3><span class="visual-card-action">เลือกโต๊ะ <span aria-hidden="true">→</span></span></div><div class="visual-card-price"><small>เริ่มต้น</small><strong>{money(rates.get(kind,0))}</strong><span>/ ชั่วโมง</span></div></div></a>''' for kind,pic,label in [('SNOOKER','table_snooker_premium.webp','Snooker'),('VIP','table_vip_premium.webp','VIP Snooker'),('POOL','table_pool_premium.webp','Pool')])
        # Keep promotion figures tied to the same hourly rates as booking quotes.
        special_rates={'SNOOKER':9000,'VIP':12000,'POOL':8000}
        promo_parts=[]
        for kind,label in [('SNOOKER','Snooker'),('VIP','VIP'),('POOL','Pool')]:
            if kind in rates and rates[kind]>special_rates[kind]:
                saving=rates[kind]-special_rates[kind]
                promo_parts.append(f'<span class="promo-chip">{label} ลด <strong>{money(saving)}</strong>/ชม.</span>')
        promo=f'''<section class="promo-teaser"><div class="promo-title"><span class="eyebrow">โปรโมชั่นช่วงกลางวัน</span><h2>10:00–16:00 น. · ราคาพิเศษ</h2><p>ลดเฉพาะชั่วโมงในช่วงโปรโมชั่น</p></div><div class="promo-values">{''.join(promo_parts)}</div><div class="promo-footer"><span>สมาชิก Silver ลด 5% · Gold ลด 10% · VIP ลด 15% (เฉพาะค่าบริการโต๊ะ)</span><a class="pill secondary" href="/tables">ดูโต๊ะและราคา</a></div></section>'''
        self.page(banner+'<section class="section-head"><div><h2>ประเภทโต๊ะและราคา</h2></div></section><div class="visual-grid">'+cards+'</div>'+promo,'หน้าหลัก')
    def tables(self,args):
        chosen=args.get('type','ALL').upper()
        with db.connect() as c:rows=c.execute('SELECT * FROM club_tables ORDER BY table_id').fetchall()
        linkbar=''.join(f'<a class="filter-pill {"selected" if chosen==code else ""}" href="/tables?type={code}">{name}</a>' for code,name in [('ALL','ทั้งหมด'),('SNOOKER','Snooker'),('VIP','VIP'),('POOL','Pool')])
        tablecards=''
        for r in rows:
            if chosen!='ALL' and chosen!=r['table_type']:continue
            pic='table_vip_premium.webp' if r['table_type']=='VIP' else 'table_pool_premium.webp' if r['table_type']=='POOL' else 'table_snooker_premium.webp'
            tablecards+=f'''<article class="table-card"><div class="table-image"><img src="{img(pic)}" alt="{E(r['table_code'])}" loading="lazy"><span class="table-tag">{E(r['table_type'])}</span></div><div class="table-content"><div class="card-line"><h3>{E(r['table_code'])}</h3><span class="status-good">● {"เปิดรับจอง" if r['status']=='OPEN' else 'ปิดปรับปรุง'}</span></div><div class="price-line"><strong>{money(r['hourly_rate_sat'])}</strong><small>/ ชั่วโมง</small></div><a class="pill primary block" href="/book?table={r['table_id']}">เลือกวันและเวลาจอง →</a></div></article>'''
        self.page('''<div class="page-head"><h1>เลือกโต๊ะ</h1></div><div class="filter-row">'''+linkbar+'</div><div class="table-grid">'+tablecards+'</div>','จองโต๊ะ','tables')
    def cues(self):
        with db.connect() as c:
            rows = c.execute("SELECT * FROM cues WHERE status='AVAILABLE' ORDER BY cue_id").fetchall()
        # Features derived from maker/product descriptions; actual wood grain may vary.
        specs = {
            ('Predator', 'REVO 12.4'): ('cue_revo_catalog_full.webp','พูล','คาร์บอนไฟเบอร์','ชาฟต์คาร์บอน REVO ขนาดปลาย 12.4 มม.','จุดเด่น: ลดการเบี่ยงเบนของลูกเมื่อใส่ไซด์ ช่วยให้ควบคุมตำแหน่งลูกขาวได้แม่นยำ เหมาะกับผู้เล่นพูลที่เน้นความสม่ำเสมอ','คาร์บอน • ปลาย 12.4 มม.'),
            ('Cuetec', 'Cynergy 12.5'): ('cue_cynergy_catalog_full.webp','พูล','คาร์บอนไฟเบอร์','ชาฟต์คาร์บอน CYNERGY ปลาย 12.5 มม.','จุดเด่น: ฟีลลิ่งการแทงแน่นและสม่ำเสมอ มีโครงสร้างคาร์บอนผสานแกนโฟม เหมาะกับพูลที่ต้องใช้การคุมไซด์','คาร์บอน 15K • ปลาย 12.5 มม.'),
            ('Mezz', 'EC9'): ('cue_mezz_catalog_full.webp','พูล','ชาฟต์ WX-Σ','ซีรีส์ EC9 ใช้ชาฟต์ WX-Σ ของ Mezz','จุดเด่น: เน้นความแม่นยำและความรู้สึกเป็นธรรมชาติ เหมาะกับผู้เล่นพูลที่ชอบฟีลลิ่งของไม้และการควบคุมลูก','ชาฟต์ WX-Σ • พูล'),
            ('Peradon', 'Classic'): ('cue_peradon_catalog_full.webp','สนุกเกอร์','ไม้แอช','ไม้สนุกเกอร์ดีไซน์ดั้งเดิมจากผู้ผลิตอังกฤษ','จุดเด่น: ให้สัมผัสไม้แอชตามแบบสนุกเกอร์ มีหัวเล็กสำหรับควบคุมลูกขาว แตกต่างจากไม้พูลที่มีหัวใหญ่กว่า','ไม้แอช • สนุกเกอร์'),
            ('Predator', 'Aspire'): ('cue_aspire_catalog_full.webp','พูล','ชาฟต์ไม้เมเปิล','ไม้พูล Aspire ใช้ชาฟต์ Predator ONE จากไม้เมเปิล','จุดเด่น: เน้นการควบคุมลูกด้วยชาฟต์ไม้ที่มีการเบี่ยงเบนต่ำ เหมาะกับคนที่ต้องการสัมผัสไม้ธรรมชาติ ต่างจาก REVO ที่เป็นคาร์บอน','เมเปิล ONE • พูล'),
            ("O'Min", "O'Millenium 3/4"): ('cue_omin_millenium_distinct.webp','สนุกเกอร์','ไม้แอช • จำปาเครื่องจักร','รุ่นเริ่มต้นของ O’Min ไม้แอชยาวประมาณ 57 นิ้ว หัว 10 มม. ข้อต่อ 3/4','จุดเด่น: จำปา 5 แฉกประกอบด้วยเครื่องจักร ด้ามโทนน้ำตาลดูเรียบ เหมาะกับผู้เล่นสนุกเกอร์ที่ต้องการไม้ส่วนตัวราคาเข้าถึงง่าย','จำปาเครื่องจักร • 10 มม.'),
            ("O'Min", 'Professional 3/4'): ('cue_omin_professional_distinct.webp','สนุกเกอร์','ไม้แอช • ด้ามอีโบนี','ชาฟต์แอช ด้ามอีโบนีประกอบจำปาด้วยมือ ข้อต่อทองเหลือง 3/4','จุดเด่น: งานต่อไม้แบบทำมือและด้ามสีดำตัดสีไม้ธรรมชาติ เหมาะกับผู้เล่นประจำที่ต้องการสัมผัสและบาลานซ์ที่ดีขึ้นจากรุ่นพื้นฐาน','จำปาทำมือ • ด้ามอีโบนี'),
            ("O'Min", 'Perfect 3/4'): ('cue_omin_perfect_distinct.webp','สนุกเกอร์','ไม้แอช • ลายไม้ตกแต่ง','ไม้แอชและด้ามประกอบมือ มีการเลือกไม้และงานตกแต่งจำปาตามรุ่นย่อย','จุดเด่น: งานประกอบและลวดลายด้ามที่พิถีพิถันกว่ารุ่นเริ่มต้น เหมาะกับผู้เล่นจริงจังที่ให้ความสำคัญกับความรู้สึกและงานไม้','งานประกอบมือ • ลายเด่น'),
            ('PowerGlide', 'Asas 3/4'): ('cue_powerglide_asas_distinct.webp','สนุกเกอร์','ไม้แอช • ด้ามสีดำ','ไม้แอชอเมริกาเหนือ ข้อต่อทองเหลือง 3/4 หัวหนังราว 9.5 มม.','จุดเด่น: ด้ามทำสีดำสไตล์ ebonised และข้อต่อแบบ quick release เหมาะกับผู้เล่นที่ชอบรูปลักษณ์เรียบเข้มและใช้ฝึกสนุกเกอร์สม่ำเสมอ','ด้ามดำ • หัว 9.5 มม.'),
            ('Peradon', 'Crown 3/4'): ('cue_peradon_crown_distinct.webp','สนุกเกอร์','ไม้แอช • ด้ามโรสวูด','ไม้สนุกเกอร์ผลิตในอังกฤษ ใช้ชาฟต์แอชและด้ามโรสวูด ข้อต่อ 3/4','จุดเด่น: ด้ามโรสวูดสีแดงน้ำตาลธรรมชาติ เป็นเอกลักษณ์ต่างจากด้ามดำของ PowerGlide เหมาะกับคนชอบฟีลลิ่งสนุกเกอร์คลาสสิก','โรสวูด • ผลิตอังกฤษ'),
        }
        cards=[]
        for r in rows:
            brand=str(r['brand']); model=str(r['model'])
            entry=specs.get((brand,model))
            if entry:
                filename,game,material,build,benefit,badge=entry
                picture=cue_catalog_image(r)
                content=f'<p><strong>วัสดุและโครงสร้าง</strong> {E(build)}</p><p><strong>จุดเด่นและการใช้งาน</strong> {E(benefit.removeprefix("จุดเด่น: ").removeprefix("จุดเด่น:"))}</p>'
            else:
                picture=img(r['img']);game='ไม้คิว';material='';badge='';content='<p>โปรดสอบถามข้อมูลเพิ่มเติมจากเจ้าหน้าที่</p>'
            title=E(brand+' '+model)
            cards.append(f"""<article class="cue-card cue-catalog-card cue-unique-card">
                <div class="cue-catalog-image"><img loading="lazy" src="{picture}" alt="ภาพประกอบเต็มชิ้นของ {title}"></div>
                <div class="cue-catalog-body"><h3>{title}</h3>
                <div class="cue-catalog-price"><strong>{money(r['price_sat'])}</strong><span>/ การจอง</span></div>
                <div class="cue-catalog-tags"><span>{E('ไม้พูล' if game=='พูล' else 'ไม้สนุกเกอร์' if game=='สนุกเกอร์' else game)}</span></div>
                <details class="cue-unique-reveal cue-native-detail"><summary class="cue-detail-toggle">ดูรายละเอียดไม้คิว</summary><div class="cue-detail-content">{content}</div></details>
                <a href="/tables" class="pill primary block">เลือกโต๊ะเพื่อเช่า →</a></div>
            </article>""")
        self.page('<div class="page-head compact"><h1>ไม้คิวพรีเมียม</h1><p>เลือกไม้คิวที่เหมาะกับคุณ กดดูรายละเอียดเพื่ออ่านจุดเด่นของแต่ละรุ่น</p></div><div class="cue-grid cue-catalog-grid cue-clean-grid">'+''.join(cards)+'</div>','ไม้คิวพรีเมียม','cues')
    def register_page(self):
        self.page(f'''<section class="auth-layout"><div class="auth-visual"><span>JOIN THE CLUB</span><h2>เริ่มต้นประสบการณ์<br>ที่ใช่สำหรับคุณ</h2></div><form class="auth-card" method="post" action="/register">{self.csrf()}<span class="eyebrow">CREATE ACCOUNT</span><h1>สมัครสมาชิก</h1><label>ชื่อ-นามสกุล<input name="name" required minlength="2"></label><label>อีเมล<input type="email" name="email" required></label><label>เบอร์โทร<input name="phone" required></label><label>รหัสผ่าน (6–60 ตัวอักษร)<input type="password" name="password" minlength="6" maxlength="60" required></label><button class="pill primary block">สมัครสมาชิก</button><p>มีบัญชีแล้ว? <a href="/login">เข้าสู่ระบบ</a></p></form></section>''','สมัครสมาชิก')
    def login_page(self,staff=False,args=None):
        action='/staff/login' if staff else '/login';heading='เข้าสู่ระบบผู้ดูแล' if staff else 'เข้าสู่ระบบสมาชิก'
        args=args or {}
        # Keep failed administrator sign-in feedback simple and show the same
        # login form again so the user can correct the credentials immediately.
        error_message='เข้าสู่ระบบไม่สำเร็จ' if staff and args.get('error') else ''
        error_block=alert(error_message,'error') if error_message else ''
        self.page(f'''<section class="auth-layout"><div class="auth-visual"><span>WELCOME BACK</span><h2>กลับมาสนุกกันต่อ<br>ที่ Roo Nai Gor Dai</h2></div><form class="auth-card" method="post" action="{action}">{self.csrf()}<span class="eyebrow">SECURE LOGIN</span><h1>{heading}</h1>{error_block}{'<p class="hint">ใช้บัญชีผู้ดูแลร้าน ไม่ใช่บัญชีสมาชิกทั่วไป</p>' if staff else ''}<label>{"ชื่อผู้ใช้ ADMIN" if staff else "อีเมล / เบอร์โทร / ชื่อ-นามสกุล"}<input name="identifier" required autocomplete="username"></label><label>รหัสผ่าน<input type="password" name="password" required autocomplete="current-password"></label><button class="pill primary block">เข้าสู่ระบบ</button>{'' if staff else '<p><a href="/forgot">ลืมรหัสผ่าน?</a></p>'}<p><a href="/">กลับหน้าหลัก</a></p></form></section>''','เข้าสู่ระบบ')
    def forgot_page(self):
        captcha=''
        if db.use_central_otp():
            sitekey=db.public_turnstile_sitekey()
            if sitekey and not sitekey.startswith('PASTE_'):
                # Render Turnstile explicitly so a successful challenge must fill the
                # real POST field before users can submit the password-reset request.
                captcha=(f'<div class="otp-turnstile-host" data-otp-turnstile data-sitekey="{E(sitekey)}"></div>'
                         '<input type="hidden" name="cf-turnstile-response" value="">'
                         '<p class="hint" role="status" aria-live="polite" data-otp-status>กำลังโหลดการตรวจสอบความปลอดภัย…</p>')
            else:
                captcha=alert('ระบบส่ง OTP ส่วนกลางยังตั้งค่าไม่ครบ โปรดติดต่อผู้ดูแลระบบ','error')
        form_attribute=' data-otp-forgot-form' if db.use_central_otp() else ''
        self.page(f'''<section class="form-section"><form class="auth-card" method="post" action="/forgot"{form_attribute}>{self.csrf()}<span class="eyebrow">ขั้นตอน 1/3</span><h1>ลืมรหัสผ่าน</h1><p>กรอกอีเมลเพื่อรับรหัส 8 หลัก (ใช้ได้ 10 นาที)</p><label>อีเมล<input type="email" name="email" required autocomplete="email"></label>{captcha}<button class="pill primary block">ส่งรหัสยืนยัน</button></form></section>''','ลืมรหัสผ่าน')
    def verify_page(self,args):
        self.page(f'''<section class="form-section"><form class="auth-card" method="post" action="/verify">{self.csrf()}<span class="eyebrow">ขั้นตอน 2/3</span><h1>ยืนยันรหัสจากอีเมล</h1><p>ต้องยืนยันรหัส 8 หลักให้ถูกต้องก่อนตั้งรหัสผ่านใหม่</p><label>อีเมล<input type="email" name="email" value="{E(args.get('email',''))}" required></label><label>รหัสยืนยัน<input name="code" inputmode="numeric" pattern="[0-9]{{8}}" maxlength="8" required autocomplete="one-time-code"></label><button class="pill primary block">ตรวจสอบรหัส →</button><a href="/forgot">ขอรหัสใหม่</a></form></section>''','ยืนยันรหัส')
    def reset_page(self):
        if not db.has_reset_grant(self.token):return self.go('/forgot')
        self.page(f'''<section class="form-section"><form class="auth-card" method="post" action="/reset">{self.csrf()}<span class="eyebrow">ขั้นตอน 3/3</span><h1>ตั้งรหัสผ่านใหม่</h1><p>ยืนยันอีเมลสำเร็จแล้ว ตั้งรหัสผ่านใหม่ได้ตามต้องการ (6–60 ตัวอักษร)</p><label>รหัสผ่านใหม่<input name="password" type="password" minlength="6" maxlength="60" required autocomplete="new-password"></label><label>ยืนยันรหัสผ่าน<input name="confirmation" type="password" minlength="6" maxlength="60" required autocomplete="new-password"></label><button class="pill primary block">บันทึกรหัสผ่านใหม่</button></form></section>''','ตั้งรหัสผ่านใหม่')
    def profile_page(self):
        uid=self.user_required()
        user=db.profile_details(uid)
        saved=query(self.path).get('saved')=='1'
        notice=alert('บันทึกข้อมูลโปรไฟล์สำเร็จแล้ว','success') if saved else ''
        body=f'''<div class="page-head compact"><h1>โปรไฟล์ของฉัน</h1></div>{notice}<section class="panel profile-panel"><h2>ข้อมูลบัญชี</h2>
        <form method="post" action="/profile" class="profile-form">{self.csrf()}
        <label>ชื่อ-นามสกุล<input name="name" value="{E(user['full_name'])}" minlength="2" maxlength="100" required></label>
        <label>เบอร์โทร<input name="phone" value="{E(user['phone'])}" required></label>
        <label>อีเมลที่ลงทะเบียน<input type="email" name="email" value="{E(user['email'])}" readonly></label>
        <label>รหัสผ่านปัจจุบัน (กรอกเพื่อยืนยันการบันทึก)
          <span class="password-field"><input id="current-password" name="password" type="password" autocomplete="current-password" placeholder="กรอกรหัสผ่านที่ใช้เข้าสู่ระบบ" required><button type="button" class="password-toggle" data-password-toggle="current-password" aria-pressed="false" aria-label="แสดงรหัสผ่านที่กรอก">แสดง</button></span>
          <small class="profile-password-hint">เพื่อความปลอดภัย ระบบไม่แสดงรหัสผ่านที่บันทึกไว้ แต่สามารถกด “แสดง” เพื่อดูรหัสที่คุณพิมพ์ได้</small>
        </label>
        <div class="profile-save-actions"><button class="pill primary profile-save-button" type="submit">บันทึกข้อมูล</button></div></form>
        <div class="profile-security"><h2>ความปลอดภัย</h2><p>เปลี่ยนรหัสผ่านโดยยืนยัน OTP ทางอีเมล</p>
        <a class="pill secondary" href="/forgot">เปลี่ยนรหัสผ่านผ่านอีเมล →</a></div></section>'''
        self.page(body,'โปรไฟล์ของฉัน','my')
    def post_profile(self,f,files):
        uid=self.user_required()
        db.update_profile(uid,f.get('name',''),f.get('email',''),f.get('phone',''),f.get('password',''))
        self.go('/profile?saved=1')
    def book_page(self,args):
        uid=self.user_required();tid=int(args.get('table','0'))
        with db.connect() as c:
            table=c.execute('SELECT * FROM club_tables WHERE table_id=?',(tid,)).fetchone()
            cues=c.execute('SELECT * FROM cues WHERE status="AVAILABLE" ORDER BY cue_id').fetchall()
            credit=db.credit_balance(c,uid);member=db.membership(c,uid)
        if not table:raise db.RuleError('ไม่พบโต๊ะที่เลือก')
        from datetime import timedelta
        earliest=db.now()+timedelta(minutes=30)
        rounded=(earliest.minute//30 + (1 if earliest.minute%30 else 0))*30
        earliest=earliest.replace(minute=0,second=0,microsecond=0)+timedelta(minutes=rounded)
        if 2<=earliest.hour<10:earliest=earliest.replace(hour=10,minute=0)
        date=earliest.strftime('%Y-%m-%d')
        selected_time=earliest.strftime('%H:%M')
        slots=[]
        for hour in list(range(10,24))+list(range(0,2)):
            for minute in (0,30):
                value=f'{hour:02d}:{minute:02d}'
                slots.append(f'<option value="{value}" {"selected" if value==selected_time else ""}>{value} น.</option>')
        cue_cards=''.join(f'''<label class="cue-choice cue-choice-refreshed"><input type="checkbox" name="cue_ids" value="{cue['cue_id']}" data-price-sat="{cue['price_sat']}"><img src="{cue_catalog_image(cue)}" alt="{E(cue['brand'])} {E(cue['model'])}"><span class="cue-choice-copy"><strong>{E(cue['brand'])} {E(cue['model'])}</strong><small>{money(cue['price_sat'])} / การจอง</small></span><span class="choose-mark">เลือก</span></label>''' for cue in cues)
        # Credit is a monetary wallet; membership points use a separate scale.
        credit_limit=max(0,credit//100)
        section=f'''<div class="page-head compact booking-head"><span class="eyebrow">จองโต๊ะออนไลน์</span><h1>{E(table['table_code'])}</h1></div>
        <div class="two-col booking-layout"><section class="panel booking-main"><form class="booking-form modern-book-form" method="post" action="/book" data-quote-form>{self.csrf()}<input type="hidden" name="table_id" value="{tid}">
        <section class="booking-stage" data-booking-step="1"><div class="step-heading"><span class="step-number">01</span><div><h2>เลือกวันและเวลา</h2></div></div>
        <div class="form-grid booking-date-time"><label>วันที่เล่น<div class="date-picker-wrap" data-calendar-host><input type="date" name="date" value="{date}" min="{date}" required></div></label><label>เวลาเริ่ม<select name="time" required>{''.join(slots)}</select></label></div>
        <label>จำนวนชั่วโมง<select name="hours">{''.join(f'<option value="{x}">{x} ชั่วโมง</option>' for x in range(1,7))}</select></label>
        <div class="booking-stage-foot"><button type="button" class="pill primary booking-next" data-booking-next>ตรวจสอบราคา →</button></div></section>
        <section class="booking-stage booking-review" data-booking-step="2"><div class="step-heading"><span class="step-number">02</span><div><h2>ตรวจสอบและยืนยัน</h2></div></div>
        <p class="booking-choice-summary" data-booking-choice aria-live="polite"></p>
        <details class="booking-extras"><summary>เพิ่มไม้คิวหรือใช้ส่วนลด <span class="optional-label">ไม่บังคับ</span></summary><div class="booking-extras-inner">
        <h3>ไม้คิว (ไม่บังคับ)</h3><div class="cue-choice-grid">{cue_cards}</div>
        {f'<div class="compact-benefit"><strong>Booking Credit มี {money(credit)}</strong><span>1 เครดิต = 1 บาท</span><label>ใช้เครดิต (บาท)<input name="credit" type="number" min="0" max="{credit_limit}" step="1" value="0" inputmode="numeric"></label></div>' if credit_limit>0 else '<p class="empty-benefit">Booking Credit: ยังไม่มีเครดิต (1 เครดิต = 1 บาท)</p><input type="hidden" name="credit" value="0">'}
        {f'<div class="compact-benefit"><strong>คะแนนสะสม {member["available_points"]:,} คะแนน</strong><span>500 คะแนน = ลด 50 บาท</span><label>ใช้คะแนน<select name="redeem_points">{''.join(f'<option value="{i*500}">{i*500} คะแนน → ลด {i*50} บาท</option>' for i in range(min(member["available_points"]//500,10)+1))}</select></label></div>' if member['available_points']>=500 else f'<p class="empty-benefit">คะแนนสะสม {member["available_points"]:,} คะแนน · ครบ 500 คะแนน ลด 50 บาท</p><input type="hidden" name="redeem_points" value="0">'}
        </div></details>
        <section class="checkout-summary"><h3>สรุปราคา</h3><div class="live-quote" data-quote-panel role="status" aria-live="polite">กำลังตรวจสอบราคา…</div></section>
        <div class="booking-confirm-actions"><button type="button" class="pill secondary" data-booking-back>แก้ไขวันและเวลา</button><button type="submit" class="pill primary booking-submit">ยืนยันและไปชำระเงิน →</button></div></section></form></section>
        <aside class="panel booking-side"><img class="wide-image" src="{img('table_vip_premium.webp' if table['table_type']=='VIP' else 'table_pool_premium.webp' if table['table_type']=='POOL' else 'table_snooker_premium.webp')}" alt="ภาพโต๊ะ"><h2>{E(table['table_code'])}</h2><p>ราคา {money(table['hourly_rate_sat'])} / ชั่วโมง</p></aside></div>'''
        self.page(section,'จองโต๊ะ','tables')
    def quote_json(self,args):
        uid=self.user_required()
        try:
            start=f'{args.get("date","")} {args.get("time","")}:00'
            cue_ids=[int(x) for x in args.get('cue_ids','').split(',') if x.strip()]
            with db.connect() as c:
                q=db.quote(c,int(args.get('table','0')),start,int(args.get('hours','1')),user_id=uid,cue_ids=cue_ids,redeem_points=int(args.get('redeem_points','0')))
                balance=db.credit_balance(c,uid)
            requested=int(args.get('credit','0'))*100
            if requested<0 or requested>balance or requested>q['total_sat']:raise db.RuleError('เครดิตที่ใช้เกินยอดที่มีหรือเกินราคา')
            result={key:q[key] for key in ('table_fee_sat','cue_fee_sat','discount_sat','member_discount_sat','points_discount_sat','redeemed_points','total_sat')}
            result.update({'due_sat':q['total_sat']-requested,'credit_sat':requested,'points_earned_after_approval':(q['total_sat']-requested)//1000,'no_show_credit_refund_sat':q['total_sat']})
            self.send(json.dumps(result,ensure_ascii=False),kind='application/json; charset=utf-8',headers={'Cache-Control':'no-store'})
        except (ValueError,TypeError,db.RuleError) as exc:
            self.send(json.dumps({'error':str(exc)},ensure_ascii=False),code=400,kind='application/json; charset=utf-8')
    def my_bookings(self):
        uid=self.user_required()
        with db.connect() as c:rows=c.execute('''SELECT b.*,t.table_code,(SELECT status FROM payment_slips WHERE booking_id=b.booking_id ORDER BY slip_id DESC LIMIT 1) slip_status FROM bookings b JOIN club_tables t ON t.table_id=b.table_id WHERE b.user_id=? ORDER BY b.booking_id DESC LIMIT 100''',(uid,)).fetchall();credit=db.credit_balance(c,uid);member=db.membership(c,uid)
        cards=''
        for b in rows:
            action=f'<a class="pill primary" href="/slip?id={b["booking_id"]}">{"เปลี่ยนสลิป" if b["status"]=="UNDER_REVIEW" else "แนบสลิป"}</a>' if b['status'] in ('PENDING_SLIP','REJECTED','UNDER_REVIEW') else f'<a class="pill secondary" href="/receipt?id={b["booking_id"]}">ดูใบเสร็จ</a>' if b['status'] in ('CONFIRMED','CHECKED_IN','COMPLETED','NO_SHOW') else ''
            cancel=f'<form method="post" action="/cancel">{self.csrf()}<input type="hidden" name="id" value="{b["booking_id"]}"><button class="pill ghost" data-confirm="ยืนยันยกเลิกรายการนี้หรือไม่? หากโอนเงินจริงไปแล้ว กรุณาติดต่อร้านเพื่อดำเนินการคืนเงิน">ยกเลิกรายการ</button></form>' if b['status'] in ('PENDING_SLIP','REJECTED') else ''
            reject_notice='<div class="alert error" role="alert">สลิปไม่ผ่านการตรวจสอบ กรุณาแนบสลิปใหม่หรือยกเลิกรายการ</div>' if b['status']=='REJECTED' else ''
            cards+=f'''<article class="booking-row"><div><b>#{b['booking_id']} · {E(b['table_code'])}</b><p>{E(b['start_at'][:16])} – {E(b['end_at'][11:16])}</p><div class="my-booking-amount">ค่าจอง <strong>{money(b['total_sat'])}</strong> <span>· เครดิตที่ใช้ {money(b['credit_sat'])}</span></div></div><div><span class="booking-status">{E(thai_status(b))}</span>{reject_notice}<div class="actions">{action}{cancel}</div></div></article>'''
        self.page(f'<div class="page-head"><span class="eyebrow">MY BOOKINGS</span><h1>รายการจองของฉัน</h1><div class="my-benefit-summary"><div><span>Booking Credit คงเหลือ</span><strong>{money(credit)}</strong><small>1 เครดิต = 1 บาท</small></div><div><span>คะแนนสะสมที่ใช้ได้</span><strong>{member["available_points"]:,} คะแนน</strong><small>500 คะแนน = ส่วนลด 50 บาท</small></div><div><span>ระดับสมาชิกปัจจุบัน</span><strong>{E(member["name"])}</strong><small>คะแนนสะสมรวม {member["points"]:,} คะแนน</small></div></div></div><div class="booking-list">'+(cards or '<div class="empty">ยังไม่มีรายการจอง <a href="/tables">ไปเลือกโต๊ะ</a></div>')+'</div>','รายการจองของฉัน','my')
    def slip_page(self,args):
        uid=self.user_required();bid=int(args.get('id','0'))
        with db.connect() as c:
            b=c.execute("SELECT b.*,t.table_code FROM bookings b JOIN club_tables t ON t.table_id=b.table_id WHERE booking_id=? AND user_id=?",(bid,uid)).fetchone()
            pending=c.execute("SELECT slip_id FROM payment_slips WHERE booking_id=? AND status='PENDING' ORDER BY slip_id DESC LIMIT 1",(bid,)).fetchone()
        if not b or b['status'] not in ('PENDING_SLIP','REJECTED','UNDER_REVIEW'):
            raise db.RuleError('ไม่พบรายการที่แนบหรือแก้ไขสลิปได้')
        payee=payee_settings()
        demo=os.environ.get('RNGD_TEST_MODE')=='1'
        if demo:notice=alert('กำลังเปิดในโหมดทดสอบ ห้ามโอนเงินจริง','info')
        else:notice=alert('โปรดตรวจสอบชื่อผู้รับเงินในแอปธนาคารให้ตรงก่อนยืนยันการโอน','info')
        if demo:
            bank_info='<div class="info">โหมดทดสอบ: ไม่มีการรับโอนเงินจริง · ใช้สลิปตัวอย่างเพื่อทดลองระบบเท่านั้น</div>'
        elif payee['configured']:
            bank_info=(f'<div class="bank-info"><small>บัญชีรับโอนของร้าน {E(payee["shop"])}</small><h3>{E(payee["bank"])}</h3><strong>{E(payee["name"])}</strong><p>เลขบัญชี {E(payee["account"])}</p></div><img class="payment-qr" src="/payee-qr" alt="QR พร้อมเพย์บัญชีของร้าน">')
        else:
            bank_info=alert('ยังไม่ได้ติดตั้ง QR และข้อมูลบัญชีของร้าน','error')
        replacing=b['status']=='UNDER_REVIEW'
        status_note='<div class="info">สลิปก่อนหน้านี้กำลังรอตรวจ คุณสามารถเปลี่ยนรูปได้ก่อนที่ ADMIN จะอนุมัติ · สลิปเดิมจะถูกยกเลิกการพิจารณา</div>' if replacing else ''
        submit_label='เปลี่ยนสลิปที่ส่งแล้ว' if replacing else 'ส่งสลิปให้ ADMIN ตรวจสอบ'
        self.page(f'''<div class="page-head compact"><span class="eyebrow">ชำระเงินผ่านธนาคาร</span><h1>ชำระเงินและแนบสลิป · การจอง #{bid}</h1><p>{E(b['table_code'])} · {E(b['start_at'][:16])}</p></div><div class="two-col payment-layout"><div class="panel"><h2>ยอดที่ต้องโอน</h2><div class="summary-line">ค่าโต๊ะ <b>{money(b['table_fee_sat'])}</b></div><div class="summary-line">ค่าเช่าไม้คิว <b>{money(b['cue_fee_sat'])}</b></div><div class="summary-line">โปรโมชันช่วงเวลา <b>−{money(b['discount_sat'])}</b></div><div class="summary-line">ส่วนลดระดับสมาชิก <b>−{money(max(0,b['table_fee_sat']+b['cue_fee_sat']-b['discount_sat']-b['points_discount_sat']-b['total_sat']))}</b></div><div class="summary-line">แลกคะแนนสมาชิก ({b['redeemed_points']} คะแนน) <b>−{money(b['points_discount_sat'])}</b></div><div class="summary-line">Booking Credit <b>−{money(b['credit_sat'])}</b></div><div class="summary-line total">ยอดโอน <strong>{money(b['due_sat'])}</strong></div>{notice}{bank_info}<p class="hint">QR อาจไม่เติมยอดให้อัตโนมัติ กรุณาใส่ยอดโอนให้ตรงกับรายการและตรวจชื่อผู้รับในแอปธนาคาร</p></div><div class="panel"><h2>แนบหลักฐานการโอน</h2><p>หลังโอนจริง ให้แนบสลิปเพื่อส่งให้ ADMIN ตรวจสอบยอดเข้าบัญชี ก่อนยืนยันการจอง</p>{status_note}<form method="post" action="/slip" enctype="multipart/form-data" class="upload-form">{self.csrf()}<input name="id" type="hidden" value="{bid}"><label class="file-picker">สลิป JPG / PNG / WEBP ไม่เกิน 5 MB<input type="file" name="slip" accept="image/png,image/jpeg,image/webp" required></label><div class="upload-preview-wrap"><img class="upload-preview" alt="สลิปที่เลือก" hidden><button type="button" class="pill ghost small" data-clear-slip hidden>ล้างรูป / เลือกใหม่</button></div><button type="submit" class="pill primary block" {'disabled' if not (demo or payee['configured']) else ''}>{submit_label}</button></form><p class="hint">หลัง ADMIN ยืนยันรายการนี้ จะได้รับ {b['due_sat']//1000} คะแนนสมาชิก · Booking Credit เพิ่มตอนนี้ 0 บาท (หากชำระแล้วไม่มาเช็กอินเกิน 15 นาที จะคืน {money(b['total_sat'])} เป็นเครดิต)</p><p class="hint">ส่งสลิปแล้ว ≠ ชำระสำเร็จ · การอนุมัติต้องทำโดย ADMIN หลังตรวจธุรกรรมในบัญชีธนาคาร</p></div></div>''','ชำระเงิน','my')
    def receipt_page(self,args):
        uid=self.user_required();bid=int(args.get('id','0'))
        with db.connect() as c:b=c.execute('''SELECT b.*,t.table_code FROM bookings b JOIN club_tables t ON t.table_id=b.table_id WHERE booking_id=? AND user_id=?''',(bid,uid)).fetchone()
        if not b or b['status'] not in ('CONFIRMED','CHECKED_IN','COMPLETED','NO_SHOW'):raise db.RuleError('ไม่มีใบเสร็จสำหรับรายการนี้')
        self.page(f'''<div class="receipt"><img src="{img('snooker_red_ball.png')}" alt="โลโก้ร้าน"><span class="eyebrow">ROO NAI GOR DAI</span><h1>หลักฐานยืนยันการจอง</h1><div class="receipt-note">เอกสารจากระบบหลัง ADMIN ตรวจยืนยันสลิป ไม่ใช่ใบกำกับภาษีหรือหลักฐานการโอนจากธนาคาร</div><hr><p>หมายเลข: RNGD-WEB-{bid:06d}</p><p>โต๊ะ: {E(b['table_code'])}</p><p>วันเวลา: {E(b['start_at'])}</p><p>ราคารวม: {money(b['total_sat'])}</p><p>ใช้ Booking Credit: {money(b['credit_sat'])}</p><h2>ยอดโอน: {money(b['due_sat'])}</h2><div class="receipt-actions"><button class="pill primary" data-action="print">พิมพ์ใบเสร็จ</button><a class="pill secondary" href="/my">← กลับรายการของฉัน</a></div></div>''','ใบเสร็จ','my')
    def admin_status_json(self):
        self.staff_required()
        with db.connect() as c:
            pending=c.execute("SELECT count(*) FROM payment_slips WHERE status='PENDING'").fetchone()[0]
            checkins=c.execute("SELECT count(*) FROM bookings WHERE status='CONFIRMED' AND start_at<=? AND datetime(start_at,'+15 minutes')>=?",(db.moment(),db.moment())).fetchone()[0]
            checkouts=c.execute("SELECT count(*) FROM bookings WHERE status='CHECKED_IN' AND end_at<=?",(db.moment(),)).fetchone()[0]
        self.send(__import__('json').dumps({'pending':pending,'checkins':checkins,'checkouts':checkouts}),kind='application/json; charset=utf-8',headers={'Cache-Control':'no-store'})
    def admin_subnav(self, selected='overview'):
        tabs=[('overview','ภาพรวม','/admin'),('booking','การจอง','/admin?view=booking'),('payments','ตรวจสลิป','/admin?view=payments'),('revenue','รายได้','/admin?view=revenue'),('tables','จัดการโต๊ะ','/admin?view=tables'),('users','จัดการผู้ใช้','/admin/users'),('admins','จัดการ ADMIN','/admin/admins')]
        return ''.join(f'<a class="filter-pill {"selected" if selected==key else ""}" href="{url}">{E(label)}</a>' for key,label,url in tabs)

    def admin_header(self, selected='overview'):
        with db.connect() as c:
            waiting=c.execute("SELECT COUNT(*) FROM payment_slips WHERE status='PENDING'").fetchone()[0]
        return (f'<div class="page-head"><h1>ศูนย์จัดการร้าน</h1>'
                f'<p>การจอง · ตรวจสลิป · จัดการโต๊ะและสมาชิก</p></div>'
                f'<div class="admin-live-alert" data-admin-alert role="status">'
                f'<span>รอตรวจสลิป {waiting} รายการ</span>'
                f'<a href="/admin?view=payments">เปิดคิวตรวจสอบ →</a></div>'
                f'<div class="admin-nav">{self.admin_subnav(selected)}</div>')

    def admin_page(self,args):
        self.staff_required()
        section=args.get('view','overview')
        links=self.admin_subnav(section)
        with db.connect() as c:
            cnt=c.execute('SELECT count(*) FROM bookings').fetchone()[0]
            pending=c.execute("SELECT count(*) FROM payment_slips WHERE status='PENDING'").fetchone()[0]
        lookup_args=dict(args)
        if section in ('booking','payments'):
            records,total,page,mode,state_filter=admin_search_rows(section,lookup_args)
            bookings=records if section=='booking' else []
            slips=records if section=='payments' else []
        else:
            bookings=[];slips=[];total=0;page=1;mode='booking_id';state_filter='ALL'
        def pager():
            if total==0: return '<p class="admin-result-count">ไม่พบรายการที่ตรงกับเงื่อนไข</p>'
            first=(page-1)*ADMIN_PAGE_SIZE+1;last=min(page*ADMIN_PAGE_SIZE,total)
            parts=[f'<span>แสดง {first}–{last} จาก {total} รายการ</span>']
            for step,label in ((-1,'← ก่อนหน้า'),(1,'ถัดไป →')):
                target=page+step
                if 1<=target<=(total+ADMIN_PAGE_SIZE-1)//ADMIN_PAGE_SIZE:
                    parts.append('<a class="pill secondary" href="/admin?'+E(urlencode({
                        'view':section,'search_by':mode,'q':lookup_args.get('q','')[:80],
                        'status':state_filter,'page':target}))+'">'+label+'</a>')
            return '<nav class="admin-pager" aria-label="หน้ารายการแอดมิน">'+''.join(parts)+'</nav>'
        def searchbar(view, states):
            # Keep the familiar admin filter layout while presenting booking states
            # by workflow stage. Slip IDs remain visible in table rows, but are not
            # offered as a search type: staff can find a slip via its booking ID.
            booking_groups=[
                ('รอการชำระเงิน', [('PENDING_SLIP','รอแนบสลิป'),('UNDER_REVIEW','รอตรวจสลิป'),('REJECTED','สลิปไม่ผ่าน')]),
                ('กำลังใช้บริการ', [('CONFIRMED','ยืนยันแล้ว · รอเช็กอิน'),('CHECKED_IN','กำลังเล่น · เช็กอินแล้ว')]),
                ('สิ้นสุดรายการ', [('COMPLETED','เสร็จสิ้น'),('NO_SHOW','ไม่มาตามนัด'),('CANCELLED','ยกเลิก')]),
            ]
            def option(code,label):
                return f'<option value="{code}" {"selected" if state_filter==code else ""}>{label}</option>'
            if view=='booking':
                opts=option('ALL','ทุกสถานะ')+''.join(
                    '<optgroup label="'+title+'">'+''.join(option(code,label) for code,label in members)+'</optgroup>'
                    for title,members in booking_groups)
                status_label='สถานะการจอง'
                status_help='<small class="admin-status-help">เลือกตามขั้นตอน: รอชำระเงิน / กำลังใช้บริการ / สิ้นสุด</small>'
            else:
                opts=''.join(option(code,label) for code,label in states)
                status_label='ผลตรวจสลิป'
                status_help=''
            modes=[('booking_id','เลขจอง'),('table','โต๊ะ'),('customer','ชื่อลูกค้า')]
            modeopts=''.join(f'<option value="{k}" {"selected" if mode==k else ""}>{label}</option>' for k,label in modes)
            return (f'<form class="admin-filters admin-search-clear" method="get" action="/admin">'
                    f'<input type="hidden" name="view" value="{view}">'
                    f'<label>ค้นหาจาก<select name="search_by" aria-label="ประเภทการค้นหา">{modeopts}</select></label>'
                    f'<label>คำค้น<input name="q" placeholder="เลขจอง เลขโต๊ะ หรือชื่อลูกค้า" value="{E(args.get("q","")[:80])}"></label>'
                    f'<label class="admin-status-field">{status_label}<select name="status" aria-label="{status_label}">{opts}</select>{status_help}</label>'
                    f'<div class="admin-filter-actions"><button class="pill primary" type="submit">ค้นหา</button>'
                    f'<a class="pill secondary" href="/admin?view={view}">ล้างตัวกรอง</a></div></form>')
        top=self.admin_header(section)
        if args.get('error'):top+=alert(args['error'][:240],'error')
        if section=='overview':
            options=[('all','ทั้งหมด'),('today','วันนี้'),('week','7 วันล่าสุด'),('month','เดือนนี้'),('year','ปีนี้'),('custom','กำหนดวันเอง')]
            period=args.get('period','all')
            if period not in dict(options):period='all'
            today=db.now().date()
            start=end=None
            if period=='today':start=end=today
            elif period=='week':start=today-timedelta(days=6);end=today
            elif period=='month':start=today.replace(day=1);end=today
            elif period=='year':start=today.replace(month=1,day=1);end=today
            elif period=='custom':
                try:
                    start=date.fromisoformat(args.get('from',''))
                    end=date.fromisoformat(args.get('to',''))
                    if start>end:raise ValueError('reversed')
                except (ValueError,TypeError):
                    start=end=today
                    top+=alert('ช่วงวันที่ไม่ถูกต้อง จึงแสดงข้อมูลวันนี้แทน','error')
            def condition(column):
                if start is None:return '',[]
                return f' WHERE {column} >= ? AND {column} < ?', [start.isoformat()+' 00:00:00',(end+timedelta(days=1)).isoformat()+' 00:00:00']
            booking_where,booking_params=condition('start_at')
            pending_where,pending_params=condition('uploaded_at')
            revenue_where,revenue_params=condition('reviewed_at')
            with db.connect() as c:
                cnt=c.execute('SELECT COUNT(*) FROM bookings'+booking_where,booking_params).fetchone()[0]
                pending=c.execute("SELECT COUNT(*) FROM payment_slips"+(" WHERE status='PENDING' AND "+pending_where[7:] if pending_where else " WHERE status='PENDING'"),pending_params).fetchone()[0]
                revenue=c.execute("SELECT COALESCE(SUM(amount_sat),0) FROM payment_slips"+(" WHERE status='APPROVED' AND "+revenue_where[7:] if revenue_where else " WHERE status='APPROVED'"),revenue_params).fetchone()[0]
            opts=''.join(f'<option value="{code}" {"selected" if period==code else ""}>{label}</option>' for code,label in options)
            custom_hidden=' hidden' if period!='custom' else ''
            date_fields=(f'<div class="admin-custom-dates booking-date-time" data-admin-custom-dates{custom_hidden}>'
                         f'<label>ตั้งแต่<div class="date-picker-wrap"><input type="date" name="from" value="{E(start.isoformat() if period=="custom" else args.get("from",""))}"></div></label>'
                         f'<label>ถึง<div class="date-picker-wrap"><input type="date" name="to" value="{E(end.isoformat() if period=="custom" else args.get("to",""))}"></div></label>'
                         f'</div>')
            content=(f'<form class="admin-filters admin-overview-filters" method="get" action="/admin" data-admin-period-form><input type="hidden" name="view" value="overview"><label>ช่วงเวลาภาพรวม<select name="period" data-admin-period-select>{opts}</select></label>{date_fields}'
                     f'<div class="admin-filter-actions"><button class="pill primary" type="submit">แสดงข้อมูล</button></div></form>'
                     f'<div class="info">สถิติการจองอ้างอิงจากวันเวลาใช้งานที่จองไว้ · สลิปรอตรวจอ้างอิงวันที่อัปโหลด · รายได้รวมอ้างอิงวันที่ ADMIN อนุมัติสลิป</div>'
                     f'<div class="stat-grid"><div class="stat-card"><strong>{cnt}</strong><span>การจองในช่วงที่เลือก</span></div>'
                     f'<div class="stat-card"><strong>{pending}</strong><span>สลิปรอตรวจในช่วงที่เลือก</span></div>'
                     f'<div class="stat-card"><strong>{money(revenue)}</strong><span>รายได้รวมในช่วงที่เลือก</span></div></div>'
                     f'<div class="info"><a class="pill primary" href="/admin?view=booking&status=CONFIRMED">รายการรอ Check-in</a> '
                     f'<a class="pill secondary" href="/admin?view=booking&status=CHECKED_IN">รายการรอ Check-out</a></div>'
                     f'<div class="info"><b>หมายเหตุ:</b> รายได้รวมหมายถึงยอดสลิปที่ ADMIN อนุมัติแล้ว ไม่ใช่กำไรสุทธิ และไม่ได้หักยอดคืนเครดิตภายหลัง</div>')
        elif section=='revenue':
            with db.connect() as c:
                approved=c.execute("SELECT COALESCE(SUM(amount_sat),0) FROM payment_slips WHERE status='APPROVED'").fetchone()[0]
                transactions=c.execute("SELECT p.*,b.start_at,t.table_code FROM payment_slips p JOIN bookings b ON b.booking_id=p.booking_id JOIN club_tables t ON t.table_id=b.table_id WHERE p.status='APPROVED' ORDER BY p.reviewed_at DESC LIMIT 100").fetchall()
            rows=''.join(f'<tr><td>#{r["slip_id"]}</td><td>#{r["booking_id"]}</td><td>{E(r["table_code"])}</td><td>{E(r["reviewed_at"])}</td><td>{money(r["amount_sat"])}</td></tr>' for r in transactions)
            label='ยอดจำลองที่อนุมัติแล้ว' if os.environ.get('RNGD_TEST_MODE')=='1' else 'ยอดที่ ADMIN ยืนยันจากสลิปแล้ว'
            content=f'<div class="stat-grid"><div class="stat-card"><strong>{money(approved)}</strong><span>{label}</span></div><div class="stat-card"><strong>{len(transactions)}</strong><span>รายการอนุมัติที่แสดง</span></div></div><div class="info">รายงานนี้อ้างอิงรายการสลิปที่ ADMIN กดอนุมัติ ไม่ใช่ผลกระทบยอดจากธนาคารแบบอัตโนมัติ</div><div class="table-scroll"><table><thead><tr><th>สลิป</th><th>การจอง</th><th>โต๊ะ</th><th>ตรวจเมื่อ</th><th>ยอด</th></tr></thead><tbody>{rows}</tbody></table></div>'
        elif section=='tables':
            content=self.admin_tables_view()
        elif section=='payments':
            rows=''
            for s in slips:
                controls=(f'<a class="pill secondary" href="/admin/slip-view?id={s["slip_id"]}">ดูภาพ</a><form method="post" action="/admin/review">{self.csrf()}<input type="hidden" name="slip_id" value="{s["slip_id"]}"><input type="hidden" name="decision" value="approve"><button class="pill primary" data-confirm="ตรวจยอดเงินจริงในบัญชีธนาคารแล้วและต้องการยืนยันสลิปนี้ใช่หรือไม่?">ยืนยันรับเงิน</button></form><form method="post" action="/admin/review">{self.csrf()}<input type="hidden" name="slip_id" value="{s["slip_id"]}"><input type="hidden" name="decision" value="reject"><button class="pill danger" data-confirm="ปฏิเสธสลิปนี้?">ปฏิเสธ</button></form>' if s['status']=='PENDING' else E(s['status']))
                rows+=f'''<tr><td>#{s['slip_id']}</td><td>#{s['booking_id']}</td><td>{E(s['table_code'])}</td><td>{E(s['full_name'])}</td><td>{money(s['amount_sat'])}</td><td>{E(s['status'])}</td><td><div class="actions">{controls}</div></td></tr>'''
            content='<h2>ตรวจสอบสลิปโอนเงิน</h2><p>กรุณาเทียบวัน เวลา ชื่อบัญชี และยอดเงินจริงจากแอปธนาคารก่อนอนุมัติ</p>'+searchbar('payments',[('ALL','ทั้งหมด'),('PENDING','รอตรวจ'),('APPROVED','อนุมัติ'),('REJECTED','ปฏิเสธ'),('REPLACED','สลิปถูกเปลี่ยน')])+'<div class="table-scroll"><table><thead><tr><th>สลิป</th><th>จอง</th><th>โต๊ะ</th><th>ลูกค้า</th><th>ยอด</th><th>สถานะ</th><th>จัดการ</th></tr></thead><tbody>'+rows+'</tbody></table></div>'+pager()
        else:
            rows=''.join(f'<tr><td>#{b["booking_id"]}</td><td>{E(b["table_code"])}</td><td>{E(b["start_at"][:16])}</td><td>{E(b["full_name"])}</td><td>{E(thai_status(b))}{self.countdown_html(b)}</td><td>{money(b["total_sat"])}</td><td>{self.admin_actions(b)}</td></tr>' for b in bookings)
            content='<h2>การจองและเช็กอิน / เช็กเอาต์</h2><div class="info">ปุ่มเช็กอินจะแสดงเมื่อจองได้รับการยืนยันแล้ว และปุ่มเช็กเอาต์จะแสดงเมื่อเช็กอินสำเร็จ</div>'+searchbar('booking',[('ALL','ทั้งหมด'),('PENDING_SLIP','รอแนบสลิป'),('UNDER_REVIEW','รอตรวจสลิป'),('REJECTED','สลิปไม่ผ่าน'),('CONFIRMED','ยืนยันแล้ว'),('CHECKED_IN','กำลังเล่น'),('COMPLETED','เสร็จสิ้น'),('NO_SHOW','ไม่มาตามนัด'),('CANCELLED','ยกเลิก')])+'<div class="table-scroll"><table><thead><tr><th>เลขจอง</th><th>โต๊ะ</th><th>เวลา</th><th>ลูกค้า</th><th>สถานะ</th><th>ราคา</th><th>จัดการ</th></tr></thead><tbody>'+rows+'</tbody></table></div>'+pager()
        self.page(top+content,'ศูนย์จัดการร้าน',section if section in ('overview','booking','payments','revenue','tables') else 'overview')
    def admin_tables_view(self):
        self.staff_required(admin=True)
        with db.connect() as c:
            tables=c.execute('SELECT * FROM club_tables ORDER BY table_id').fetchall()
            active=c.execute("SELECT table_id,COUNT(*) AS n FROM bookings WHERE status IN ('CONFIRMED','CHECKED_IN','UNDER_REVIEW') GROUP BY table_id").fetchall()
        busy={r['table_id']:r['n'] for r in active}
        rows=[]
        for t in tables:
            state='เปิดจอง' if t['status']=='OPEN' else 'ปิดปรับปรุง'
            disable='disabled title="มีรายการจองค้างอยู่"' if busy.get(t['table_id']) and t['status']=='OPEN' else ''
            action='CLOSE' if t['status']=='OPEN' else 'OPEN'
            button='ปิดปรับปรุง' if action=='CLOSE' else 'เปิดจอง'
            form=f'<form method="post" action="/admin/table-update">{self.csrf()}<input type="hidden" name="id" value="{t["table_id"]}"><input type="hidden" name="action" value="{action}"><button class="pill secondary small" {disable}>{button}</button></form>'
            rows.append(f'<tr><td>{E(t["table_code"])}</td><td>{E(t["table_type"])}</td><td>{money(t["hourly_rate_sat"])}/ชม.</td><td>{state}</td><td>{busy.get(t["table_id"],0)} รายการ</td><td>{form}</td></tr>')
        return '<h2>จัดการโต๊ะ</h2><p>เปิดจองหรือปิดปรับปรุงโต๊ะ โดยไม่กระทบการจองที่มีอยู่</p><div class="table-scroll"><table><thead><tr><th>โต๊ะ</th><th>ประเภท</th><th>ราคา</th><th>สถานะ</th><th>จองค้าง</th><th>จัดการ</th></tr></thead><tbody>'+''.join(rows)+'</tbody></table></div>'
    def post_table_update(self,f,files):
        self.staff_required(admin=True)
        try: tid=int(f.get('id','0'))
        except ValueError:raise db.RuleError('หมายเลขโต๊ะไม่ถูกต้อง')
        action=f.get('action','')
        if action not in ('CLOSE','OPEN'):raise db.RuleError('คำสั่งไม่ถูกต้อง')
        with db.connect(True) as c:
            table=c.execute('SELECT status FROM club_tables WHERE table_id=?',(tid,)).fetchone()
            if table is None:raise db.RuleError('ไม่พบโต๊ะ')
            if action=='CLOSE':
                blocked=c.execute("SELECT 1 FROM bookings WHERE table_id=? AND status IN ('CONFIRMED','CHECKED_IN','UNDER_REVIEW') LIMIT 1",(tid,)).fetchone()
                if blocked:raise db.RuleError('มีการจองที่ยังดำเนินการอยู่ ไม่สามารถปิดโต๊ะนี้ได้')
            c.execute('UPDATE club_tables SET status=? WHERE table_id=?',('OPEN' if action=='OPEN' else 'MAINTENANCE',tid))
        self.go('/admin?view=tables')
    def countdown_html(self,b):
        if b['status']=='CONFIRMED':
            return f'<div class="countdown" data-deadline="{E(b["start_at"].replace(" ","T"))}+07:00" data-grace="900">รอเวลาเริ่ม</div>'
        if b['status']=='CHECKED_IN':
            return f'<div class="countdown" data-deadline="{E(b["end_at"].replace(" ","T"))}+07:00">กำลังนับเวลา</div>'
        return ''
    def admin_actions(self,b):
        actions=[]
        if b['status']=='CONFIRMED':actions.append(f'<form method="post" action="/admin/checkin">{self.csrf()}<input name="id" type="hidden" value="{b["booking_id"]}"><button class="pill primary small">Check-in</button></form>')
        if b['status']=='CHECKED_IN':actions.append(f'<form method="post" action="/admin/checkout">{self.csrf()}<input name="id" type="hidden" value="{b["booking_id"]}"><button class="pill secondary small" data-confirm="ยืนยันว่าลูกค้าออกจากโต๊ะและคืนไม้คิวแล้ว?">Check-out</button></form>')
        if b['status'] in ('PENDING_SLIP','UNDER_REVIEW','REJECTED','CONFIRMED'):
            actions.append(f'<form method="post" action="/admin/cancel-booking">{self.csrf()}<input name="id" type="hidden" value="{b["booking_id"]}"><button class="pill danger small" data-confirm="ยืนยันยกเลิกการจอง #{b["booking_id"]} หรือไม่? ระบบจะคืนยอดที่ชำระและเครดิตที่ใช้เป็น Booking Credit (สลิปที่ยังไม่อนุมัติไม่นับเป็นเงินที่ชำระ)">ยกเลิก / คืนเครดิต</button></form>')
        return '<div class="actions">'+''.join(actions)+'</div>' if actions else '—'
    def admin_admins(self,args):
        self.staff_required(admin=True)
        with db.connect() as c:
            admins=c.execute("SELECT staff_id,username,is_active FROM staff_users ORDER BY staff_id").fetchall()
        rows=''.join(f'<tr><td>#{r["staff_id"]}</td><td>{E(r["username"])}</td><td>{"ใช้งาน" if r["is_active"] else "ปิดใช้งาน"}</td></tr>' for r in admins)
        msg=alert(args['error'][:200],'error') if args.get('error') else (alert('สร้างบัญชี ADMIN สำเร็จ','success') if args.get('created')=='1' else '')
        body=(self.admin_header('admins')+msg+'<h2>จัดการบัญชี ADMIN</h2><p>เฉพาะ ADMIN ที่เข้าสู่ระบบแล้วเท่านั้นที่สร้างบัญชีผู้ดูแลใหม่ได้</p>'
              '<div class="table-scroll"><table><thead><tr><th>ID</th><th>ชื่อผู้ใช้</th><th>สถานะ</th></tr></thead><tbody>'+rows+'</tbody></table></div>'
              '<section class="admin-form-section"><h3>เพิ่ม ADMIN ใหม่</h3><form class="admin-filters admin-admin-create" method="post" action="/admin/create-admin">'+self.csrf()
              +'<label>ชื่อผู้ใช้<input name="username" autocomplete="off" minlength="3" maxlength="60" required></label>'
              +'<label>รหัสผ่าน<input type="password" name="password" minlength="6" maxlength="60" autocomplete="new-password" required></label>'
              +'<label>ยืนยันรหัสผ่าน<input type="password" name="confirmation" minlength="6" maxlength="60" autocomplete="new-password" required></label>'
              +'<div class="admin-filter-actions"><button class="pill primary" type="submit">สร้างบัญชี ADMIN</button></div></form></section>')
        self.page(body,'จัดการ ADMIN','admins')

    def post_create_admin(self,f,files):
        actor=self.staff_required(admin=True)
        db.create_admin(actor,f.get('username',''),f.get('password',''),f.get('confirmation',''))
        self.go('/admin/admins?created=1')

    def admin_users(self,args):
        self.staff_required(admin=True)
        search=args.get('q','').strip()[:80]
        with db.connect() as c:users=c.execute('SELECT user_id,full_name,email,phone,created_at,is_active FROM users WHERE full_name LIKE ? OR email LIKE ? ORDER BY user_id DESC LIMIT 100',(f'%{search}%',f'%{search}%')).fetchall()
        trs=''
        for u in users:
            form=f'<form method="post" action="/admin/delete-user">{self.csrf()}<input type="hidden" name="id" value="{u["user_id"]}"><button class="pill danger small" data-confirm="ลบข้อมูลบัญชีผู้ใช้นี้หรือไม่? หากมีประวัติชำระเงิน ระบบจะลบข้อมูลส่วนบุคคลและเก็บเลขอ้างอิงบัญชีแบบไม่ระบุตัวตน">ลบผู้ใช้</button></form>'
            trs+=f'<tr><td>#{u["user_id"]}</td><td>{E(u["full_name"])}</td><td>{E(u["email"])}</td><td>{E(u["phone"])}</td><td>{"ใช้งาน" if u["is_active"] else "ถูกลบ"}</td><td>{form if u["is_active"] else "—"}</td></tr>'
        self.page(f'''{self.admin_header('users')}<div class="page-head"><span class="eyebrow">ADMIN · USERS</span><h1>จัดการสมาชิก</h1><p>ลบบัญชีได้ ไม่ใช่แค่ระงับการใช้งาน โดยรักษาบันทึกธุรกรรมที่จำเป็นต่อการตรวจสอบย้อนหลัง</p></div><form class="search-inline members-search" method="get" action="/admin/users"><label for="member-search">ค้นหาสมาชิก<input id="member-search" name="q" value="{E(search)}" placeholder="ชื่อหรืออีเมล" autocomplete="off"></label><button class="pill secondary" type="submit">ค้นหา</button></form><div class="table-scroll"><table><thead><tr><th>ID</th><th>ชื่อ</th><th>อีเมล</th><th>เบอร์โทร</th><th>สถานะ</th><th>ดำเนินการ</th></tr></thead><tbody>{trs}</tbody></table></div>''','จัดการสมาชิก','users')
    def payee_qr(self):
        settings=payee_settings()
        path=db.DATA/'payee_qr.png'
        if not settings['configured'] or os.environ.get('RNGD_TEST_MODE')=='1':raise db.RuleError('ไม่แสดง QR รับเงินจริงในโหมดทดสอบ')
        self.send(path.read_bytes(),kind='image/png',headers={'Cache-Control':'no-store'})
    def slip_view(self,args):
        self.staff_required(admin=True)
        sid=int(args.get('id','0'))
        with db.connect() as c:
            row=c.execute("SELECT p.slip_id,p.status,p.booking_id,p.amount_sat,t.table_code FROM payment_slips p JOIN bookings b ON b.booking_id=p.booking_id JOIN club_tables t ON t.table_id=b.table_id WHERE p.slip_id=?",(sid,)).fetchone()
        if not row:raise db.RuleError('ไม่พบสลิป')
        body=(f'<div class="slip-view-page"><div class="slip-view-head">'
              f'<h1>สลิป #{sid} · การจอง #{row["booking_id"]}</h1>'
              f'<p>{E(row["table_code"])} · {money(row["amount_sat"])} · สถานะ {E(row["status"])}</p>'
              f'<a class="pill secondary" href="/admin?view=payments">กลับไปตรวจสลิป</a></div>'
              f'<div class="slip-image-frame"><img src="/admin/slip-file?id={sid}" alt="ภาพสลิปการจอง #{row["booking_id"]}"></div></div>')
        self.page(body,'ดูสลิป','payments')

    def slip_file(self,args):
        self.staff_required(admin=True)
        slip_id=int(args.get('id','0'))
        with db.connect() as c:s=c.execute('SELECT file_name FROM payment_slips WHERE slip_id=?',(slip_id,)).fetchone()
        if not s:raise db.RuleError('ไม่พบสลิป')
        p=(db.UPLOAD_DIR/s['file_name']).resolve()
        if not p.is_relative_to(db.UPLOAD_DIR) or not p.is_file():raise db.RuleError('ไม่พบไฟล์สลิป')
        self.send(p.read_bytes(),kind='image/png',headers={'Cache-Control':'no-store'})
    def post_register(self,f,files):
        uid=db.register(f.get('name',''),f.get('email',''),f.get('phone',''),f.get('password',''));self._signin(user_id=uid);self.go('/tables')
    def _signin(self,user_id=None,staff_id=None):
        db.end_session(self.token); self.token,_=db.create_session(user_id,staff_id); self.session=db.get_session(self.token);self.cookie_new=True
    def post_login(self,f,files):self._signin(user_id=db.login(f.get('identifier',''),f.get('password','')));self.go('/my')
    def post_staff_login(self,f,files):self._signin(staff_id=db.login_staff(f.get('identifier',''),f.get('password','')));self.go('/admin')
    def post_logout(self,f,files):db.end_session(self.token);self.token,_=db.create_session();self.session=db.get_session(self.token);self.cookie_new=True;self.go('/staff/login' if urlparse(self.path).path=='/staff/logout' else '/')
    def post_forgot(self,f,files):
        # Only this route passes the human-verification token to central email delivery.
        token=f.get('cf-turnstile-response','')
        db.request_reset(f.get('email',''),send=lambda email,code: db.send_mail(email,code,captcha_token=token))
        self.go('/verify?email='+quote(f.get('email','').strip()))
    def post_verify(self,f,files):
        db.verify_reset_step(f.get('email',''),f.get('code',''),self.token)
        self.go('/reset')
    def post_reset(self,f,files):db.finish_verified_reset(self.token,f.get('password',''),f.get('confirmation',''));self.page(alert('เปลี่ยนรหัสผ่านสำเร็จ กรุณาเข้าสู่ระบบใหม่','success')+'<a class="pill primary" href="/login">เข้าสู่ระบบ</a>','ตั้งรหัสผ่านใหม่')
    def post_book(self,f,files):
        uid=self.user_required()
        try:
            start=(f.get('start') or (f.get('date','')+'T'+f.get('time',''))).replace('T',' ').strip()
            if len(start)==16:start+=':00'
            ids=[int(n) for n in f.get('cue_ids','').split(',') if n.strip()]
            bid=db.book(uid,int(f['table_id']),start,int(f['hours']),int(f['cue_id']) if f.get('cue_id') else None,int(f.get('credit','0'))*100,cue_ids=ids if 'cue_ids' in f else None,redeem_points=int(f.get('redeem_points','0')))
        except (ValueError,KeyError):raise db.RuleError('ข้อมูลการจองไม่ถูกต้อง')
        with db.connect() as c:b=c.execute('SELECT due_sat FROM bookings WHERE booking_id=?',(bid,)).fetchone()
        self.go(f'/slip?id={bid}' if b['due_sat'] else f'/receipt?id={bid}')
    def post_cancel(self,f,files):db.cancel_booking(self.user_required(),int(f.get('id',0)));self.go('/my')
    def post_slip(self,f,files):
        uid=self.user_required();bid=int(f.get('id','0'))
        if not payee_settings()['configured'] and os.environ.get('RNGD_TEST_MODE')!='1':raise db.RuleError('ยังไม่ได้ตั้งค่า QR และบัญชีรับโอนของร้าน')
        if 'slip' not in files:raise db.RuleError('กรุณาแนบรูปสลิป')
        _,raw=files['slip']
        if len(raw)>5*1024*1024:raise db.RuleError('สลิปต้องไม่เกิน 5 MB')
        try:
            imgobj=Image.open(io.BytesIO(raw));imgobj.verify()
            imgobj=Image.open(io.BytesIO(raw))
            if imgobj.format not in ('PNG','JPEG','WEBP') or imgobj.width*imgobj.height>16_000_000:raise ValueError()
            image=ImageOps.exif_transpose(imgobj).convert('RGB');out=io.BytesIO();image.save(out,format='PNG',optimize=True)
        except (UnidentifiedImageError,ValueError,OSError):raise db.RuleError('ไฟล์สลิปต้องเป็น JPG, PNG หรือ WEBP ที่ถูกต้อง')
        new_bytes=out.getvalue()
        filename=secrets.token_hex(24)+'.png';p=db.UPLOAD_DIR/filename;p.write_bytes(new_bytes)
        try:
            with db.connect() as c:
                b=c.execute('SELECT due_sat FROM bookings WHERE booking_id=? AND user_id=?',(bid,uid)).fetchone()
            if not b:raise db.RuleError('ไม่พบรายการจอง')
            db.add_slip(uid,bid,filename,b['due_sat'])
        except Exception:
            p.unlink(missing_ok=True);raise
        self.go('/my')
    def post_review(self,f,files):
        admin=self.staff_required(admin=True);sid=int(f.get('slip_id','0'));approve=f.get('decision')=='approve'
        db.review_slip(admin,sid,approve,f.get('note',''));self.go('/admin?view=payments')
    def post_admin_cancel_booking(self,f,files):
        db.admin_cancel_booking(self.staff_required(admin=True),int(f.get('id',0)))
        self.go('/admin?view=booking')
    def post_delete_user(self,f,files):db.delete_user(self.staff_required(admin=True),int(f.get('id','0')));self.go('/admin/users')
    def post_checkin(self,f,files):db.check_in(self.staff_required(),int(f.get('id','0')));self.go('/admin?view=booking')
    def post_checkout(self,f,files):db.check_out(self.staff_required(),int(f.get('id','0')));self.go('/admin?view=booking')

class ReusableHTTP(ThreadingHTTPServer):daemon_threads=True;allow_reuse_address=True

def start(host='127.0.0.1',port=8000):
    if os.environ.get('RNGD_PAYMENT_LIVE')=='1' and os.environ.get('RNGD_TEST_MODE')=='1':
        raise RuntimeError('ห้ามเปิดโหมดสาธิตและรับเงินจริงพร้อมกัน')
    if os.environ.get('RNGD_PAYMENT_LIVE')=='1' and not payee_settings()['configured']:
        raise RuntimeError('กรุณาตั้งบัญชีผู้รับโอนจริงให้ครบก่อน')
    db.init_db()
    def maintenance():
        while True:
            try:db.sweep()
            except Exception:
                if os.environ.get('RNGD_DEBUG')=='1':import traceback;traceback.print_exc()
            time.sleep(10)
    threading.Thread(target=maintenance,daemon=True,name='rngd-maintenance').start()
    print(f'Roo Nai Gor Dai Web → http://{host}:{port}')
    with ReusableHTTP((host,port),AppHandler) as http:http.serve_forever()

if __name__=='__main__':
    ap=argparse.ArgumentParser();ap.add_argument('--host',default='127.0.0.1');ap.add_argument('--port',type=int,default=8000);a=ap.parse_args();start(a.host,a.port)
