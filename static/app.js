document.querySelector('.menu-toggle')?.addEventListener('click', event => {
  const nav=document.querySelector('.navigation');
  const open=nav?.classList.toggle('open');
  event.currentTarget.setAttribute('aria-expanded',String(Boolean(open)));
});
for(const button of document.querySelectorAll('[data-confirm]')){
  button.addEventListener('click',event=>{if(!window.confirm(button.dataset.confirm))event.preventDefault();});
}
const upload=document.querySelector('input[name="slip"]');
const preview=document.querySelector('.upload-preview');
const clearSlip=document.querySelector('[data-clear-slip]');
const clearUpload=()=>{if(upload)upload.value='';if(preview){preview.removeAttribute('src');preview.hidden=true;}if(clearSlip)clearSlip.hidden=true;};
upload?.addEventListener('change',()=>{
 const file=upload.files?.[0];if(!file){clearUpload();return;}
 if(file.size>5*1024*1024||!['image/jpeg','image/png','image/webp'].includes(file.type)){
   alert('เลือกสลิป JPG, PNG หรือ WEBP ขนาดไม่เกิน 5 MB');clearUpload();return;
 }
 const reader=new FileReader();reader.onload=()=>{if(preview){preview.src=reader.result;preview.hidden=false;}if(clearSlip)clearSlip.hidden=false;};reader.readAsDataURL(file);
});
clearSlip?.addEventListener('click',()=>{clearUpload();upload?.focus();});

const bookForm=document.querySelector('[data-quote-form]');
if(bookForm){
  const output=bookForm.querySelector('[data-quote-panel]');
  const send=bookForm.querySelector('button[type="submit"],button:not([type])');
  const toMoney=sat=>new Intl.NumberFormat('th-TH',{style:'currency',currency:'THB'}).format(sat/100);
  const line=(name,content,strong=false)=>{
    const row=document.createElement('div');row.className='quote-line'+(strong?' quote-total':'');
    const label=document.createElement('span');label.textContent=name;
    const amount=document.createElement(strong?'strong':'b');amount.textContent=content;
    row.append(label,amount);output.append(row);
  };
  let lastJob=null;let active=0;
  const update=()=>{
    const serial=++active;
    if(lastJob)clearTimeout(lastJob);
    output.textContent='กำลังตรวจสอบราคาและเวลาว่าง…';if(send)send.disabled=true;
    lastJob=setTimeout(async()=>{
      const values=new FormData(bookForm);
      const params=new URLSearchParams();
      for(const key of ['table_id','date','time','hours','credit','redeem_points']) params.set(key==='table_id'?'table':key,String(values.get(key)||''));
      const selected=[...bookForm.querySelectorAll('input[name="cue_ids"]:checked')].map(x=>x.value);
      params.set('cue_ids',selected.join(','));
      try{
        const result=await fetch('/api/quote?'+params,{cache:'no-store'});
        const info=await result.json();
        if(serial!==active)return;
        output.replaceChildren();
        if(!result.ok){output.textContent=info.error||'ช่วงเวลานี้ไม่สามารถจองได้';output.classList.add('quote-error');return;}
        output.classList.remove('quote-error');
        line('ค่าโต๊ะ',toMoney(info.table_fee_sat));
        if(info.cue_fee_sat)line('ค่าไม้คิวที่เลือก',toMoney(info.cue_fee_sat));
        if(info.discount_sat)line('ลดราคาช่วง 10:00–16:00', '−'+toMoney(info.discount_sat));
        if(info.member_discount_sat)line('ส่วนลดระดับสมาชิก', '−'+toMoney(info.member_discount_sat));
        if(info.points_discount_sat)line(`แลกคะแนน (${info.redeemed_points} คะแนน)`,'−'+toMoney(info.points_discount_sat));
        if(info.credit_sat)line('ใช้ Booking Credit','−'+toMoney(info.credit_sat));
        line('ยอดที่ต้องโอน',toMoney(info.due_sat),true);
        const note=document.createElement('p');note.className='quote-note';
        note.textContent=`หลังยืนยันการชำระ ได้ ${info.points_earned_after_approval} คะแนนสมาชิก`;
        output.append(note);
        if(send)send.disabled=false;
      }catch(err){if(serial===active){output.textContent='ไม่สามารถตรวจราคาได้ กรุณาลองใหม่';output.classList.add('quote-error');}}
    },300);
  };
  for(const item of bookForm.querySelectorAll('input:not([name="csrf"]),select')){
    item.addEventListener('change',update);
    if(item.name==='credit')item.addEventListener('input',update);
  }
  update();
}
// Prevent accidental rapid double submissions without affecting form payload.
for(const form of document.querySelectorAll('form[method="post"]')){
  form.addEventListener('submit',()=>{
    const submit=form.querySelector('button[type="submit"],button:not([type])');
    if(submit){setTimeout(()=>{submit.disabled=true;},0);setTimeout(()=>{submit.disabled=false;},2500);}
  });
}
document.querySelectorAll('[data-action="print"]').forEach(b=>b.addEventListener('click',()=>window.print()));
const staffAlerts=document.querySelector('[data-admin-alert]');
if(staffAlerts){
  const poll=async()=>{
    try{
      const response=await fetch('/api/admin/status',{cache:'no-store'});if(!response.ok)return;
      const data=await response.json();
      staffAlerts.replaceChildren();
      const span=document.createElement('span');span.textContent=`รอตรวจสลิป ${Number(data.pending)} · Check-in ${Number(data.checkins)} · Check-out ${Number(data.checkouts)}`;
      const a=document.createElement('a');a.href='/admin?view=payments';a.textContent='เปิดคิวตรวจสอบ →';
      staffAlerts.append(span,a);staffAlerts.classList.toggle('attention',data.pending>0||data.checkins>0||data.checkouts>0);
    }catch(_){}
  };poll();setInterval(poll,15000);
}
const formatSeconds=n=>`${String(Math.floor(n/3600)).padStart(2,'0')}:${String(Math.floor(n%3600/60)).padStart(2,'0')}:${String(n%60).padStart(2,'0')}`;
const tick=()=>{for(const el of document.querySelectorAll('[data-deadline]')){
  const delta=Math.ceil((new Date(el.dataset.deadline).getTime()-Date.now())/1000);
  const grace=Number(el.dataset.grace||0);
  el.textContent=grace ? (delta>0?`รอเริ่ม ${formatSeconds(delta)}`:delta+grace>=0?`เช็กอินภายใน ${formatSeconds(delta+grace)}`:'เลยเวลาเช็กอิน'):(delta>=0?`เหลือเวลา ${formatSeconds(delta)}`:`หมดเวลา ${formatSeconds(-delta)} แล้ว`);
}};tick();setInterval(tick,1000);

// Accessible 2-stage booking + month/year date chooser. Falls back to native HTML date when JS is off.
(function initEasyBooking(){
  const form=document.querySelector('[data-quote-form]');
  if(!form)return;
  document.body.classList.add('js-enabled');
  const first=form.querySelector('[data-booking-step="1"]');
  const review=form.querySelector('[data-booking-step="2"]');
  const next=form.querySelector('[data-booking-next]');
  const back=form.querySelector('[data-booking-back]');
  const summary=form.querySelector('[data-booking-choice]');
  const dateField=form.querySelector('input[name="date"]');
  const timeField=form.querySelector('select[name="time"]');
  const hoursField=form.querySelector('select[name="hours"]');
  const thaiMonths=['มกราคม','กุมภาพันธ์','มีนาคม','เมษายน','พฤษภาคม','มิถุนายน','กรกฎาคม','สิงหาคม','กันยายน','ตุลาคม','พฤศจิกายน','ธันวาคม'];
  const thShort=['ม.ค.','ก.พ.','มี.ค.','เม.ย.','พ.ค.','มิ.ย.','ก.ค.','ส.ค.','ก.ย.','ต.ค.','พ.ย.','ธ.ค.'];
  const dateParts=s=>{const m=/^(\d{4})-(\d{2})-(\d{2})$/.exec(s||'');return m?{y:Number(m[1]),m:Number(m[2])-1,d:Number(m[3])}:null;};
  const iso=(y,m,d)=>`${String(y).padStart(4,'0')}-${String(m+1).padStart(2,'0')}-${String(d).padStart(2,'0')}`;
  const friendlyDate=s=>{const p=dateParts(s);return p?`${p.d} ${thShort[p.m]} ${p.y+543}`:'เลือกวันที่';};
  const summarize=()=>{if(summary)summary.textContent=`${friendlyDate(dateField.value)} · ${timeField.value} น. · ${hoursField.value} ชั่วโมง`;};
  next?.addEventListener('click',()=>{
    if(!dateField.value){dateField.focus();return;}
    if(!timeField.value){timeField.focus();return;}
    if(!hoursField.value){hoursField.focus();return;}
    summarize();first.classList.add('is-finished');review.classList.add('is-current');
    review.scrollIntoView({behavior:'smooth',block:'start'});
  });
  back?.addEventListener('click',()=>{
    first.classList.remove('is-finished');review.classList.remove('is-current');
    first.scrollIntoView({behavior:'smooth',block:'start'});
  });
  [dateField,timeField,hoursField].forEach(el=>el?.addEventListener('change',summarize));
  summarize();
  const host=form.querySelector('[data-calendar-host]');
  if(!host||!dateField)return;
  const minP=dateParts(dateField.min)||dateParts(dateField.value);
  const original=dateParts(dateField.value)||minP;
  if(!original)return;
  // Preserve native date input in DOM for submission and as graceful JS-free fallback.
  dateField.classList.add('is-enhanced');dateField.tabIndex=-1;
  const trigger=document.createElement('button');trigger.type='button';trigger.className='calendar-open-btn';
  trigger.setAttribute('aria-label','เลือกวันที่เล่นจากปฏิทิน');trigger.setAttribute('aria-expanded','false');
  const triggerLabel=document.createElement('span');
  const glyph=document.createElement('span');glyph.className='cal-symbol';glyph.textContent='▦';glyph.setAttribute('aria-hidden','true');
  trigger.append(triggerLabel,glyph);host.append(trigger);
  const panel=document.createElement('div');panel.className='cal-panel';panel.hidden=true;
  panel.setAttribute('role','dialog');panel.setAttribute('aria-label','ปฏิทินเลือกวันที่เล่น');
  const top=document.createElement('div');top.className='cal-top';
  const left=document.createElement('button');left.type='button';left.className='cal-arrow';left.textContent='‹';left.setAttribute('aria-label','เดือนก่อน');
  const monthSel=document.createElement('select');monthSel.className='cal-month';monthSel.setAttribute('aria-label','เลือกเดือน');
  thaiMonths.forEach((name,i)=>{const opt=document.createElement('option');opt.value=String(i);opt.textContent=name;monthSel.append(opt);});
  const yearSel=document.createElement('select');yearSel.className='cal-year';yearSel.setAttribute('aria-label','เลือกปี');
  const currentYear=new Date().getFullYear();
  for(let y=Math.min(minP.y,currentYear);y<=Math.max(currentYear+5,original.y+4);y++){
    const opt=document.createElement('option');opt.value=String(y);opt.textContent=String(y+543);yearSel.append(opt);
  }
  const right=document.createElement('button');right.type='button';right.className='cal-arrow';right.textContent='›';right.setAttribute('aria-label','เดือนถัดไป');
  top.append(left,monthSel,yearSel,right);panel.append(top);
  const grid=document.createElement('div');grid.className='cal-grid';panel.append(grid);
  const foot=document.createElement('div');foot.className='cal-footer';
  const cancel=document.createElement('button');cancel.type='button';cancel.textContent='ยกเลิก';
  const ok=document.createElement('button');ok.type='button';ok.textContent='เลือกวันที่';ok.className='cal-confirm';
  foot.append(cancel,ok);panel.append(foot);host.append(panel);
  let selected=dateField.value,shownY=original.y,shownM=original.m;
  const dayNames=['อา','จ','อ','พ','พฤ','ศ','ส'];
  function render(){
    monthSel.value=String(shownM);yearSel.value=String(shownY);grid.replaceChildren();
    for(const name of dayNames){const label=document.createElement('span');label.className='cal-weekday';label.textContent=name;grid.append(label);}
    const offset=new Date(shownY,shownM,1).getDay();
    const count=new Date(shownY,shownM+1,0).getDate();
    for(let i=0;i<offset;i++){const blank=document.createElement('span');grid.append(blank);}
    for(let d=1;d<=count;d++){
      const value=iso(shownY,shownM,d);
      const item=document.createElement('button');item.type='button';item.className='cal-day';item.textContent=String(d);
      item.disabled=value<(dateField.min||'0000-01-01');item.setAttribute('aria-pressed',String(selected===value));
      item.setAttribute('aria-label',`${d} ${thaiMonths[shownM]} ${shownY+543}`);
      item.addEventListener('click',()=>{selected=value;render();});grid.append(item);
    }
    left.disabled=iso(shownY,shownM,1)<iso(minP.y,minP.m,1);
    right.disabled=shownY>=currentYear+5&&shownM===11;
  }
  function open(){
    const p=dateParts(dateField.value)||original;shownY=p.y;shownM=p.m;selected=dateField.value;
    panel.hidden=false;trigger.setAttribute('aria-expanded','true');render();
    requestAnimationFrame(()=>{
      const rect=panel.getBoundingClientRect();
      if(rect.bottom>window.innerHeight-12&&host.getBoundingClientRect().top>rect.height+24){panel.style.top='auto';panel.style.bottom='calc(100% + 8px)';}
      else{panel.style.bottom='auto';panel.style.top='calc(100% + 8px)';}
    });
    monthSel.focus();
  }
  function close(){panel.hidden=true;trigger.setAttribute('aria-expanded','false');trigger.focus();}
  trigger.addEventListener('click',()=>panel.hidden?open():close());
  monthSel.addEventListener('change',()=>{shownM=Number(monthSel.value);render();});
  yearSel.addEventListener('change',()=>{shownY=Number(yearSel.value);render();});
  const shiftMonth=delta=>{const d=new Date(shownY,shownM+delta,1);shownY=d.getFullYear();shownM=d.getMonth();render();};
  left.addEventListener('click',()=>shiftMonth(-1));right.addEventListener('click',()=>shiftMonth(1));
  cancel.addEventListener('click',close);
  ok.addEventListener('click',()=>{if(selected>=dateField.min){dateField.value=selected;dateField.dispatchEvent(new Event('change',{bubbles:true}));updateLabel();close();}});
  const updateLabel=()=>{triggerLabel.textContent=friendlyDate(dateField.value);};updateLabel();
  document.addEventListener('pointerdown',event=>{if(!panel.hidden&&!host.contains(event.target)){panel.hidden=true;trigger.setAttribute('aria-expanded','false');}});
  document.addEventListener('keydown',event=>{if(event.key==='Escape'&&!panel.hidden){event.preventDefault();close();}});
  dateField.addEventListener('change',updateLabel);
})();

// Show only the password the user types; stored passwords are never reversible.
document.querySelectorAll('[data-password-toggle]').forEach(button => {
  button.addEventListener('click', () => {
    const input = document.getElementById(button.dataset.passwordToggle);
    if (!input) return;
    const shown = input.type === 'password';
    input.type = shown ? 'text' : 'password';
    button.textContent = shown ? 'ซ่อน' : 'แสดง';
    button.setAttribute('aria-pressed', String(shown));
    button.setAttribute('aria-label', shown ? 'ซ่อนรหัสผ่าน' : 'แสดงรหัสผ่าน');
  });
});


// ADMIN overview period filter: show custom date fields only when needed.
(function initAdminOverviewPeriod(){
  const form=document.querySelector('[data-admin-period-form]');
  if(!form)return;
  const select=form.querySelector('[data-admin-period-select]');
  const custom=form.querySelector('[data-admin-custom-dates]');
  if(!select||!custom)return;
  const toggle=()=>{
    const open=select.value==='custom';
    custom.hidden=!open;
    const inputs=custom.querySelectorAll('input[type="date"]');
    inputs.forEach(input=>{
      input.toggleAttribute('required',open);
      if(!open) input.classList.remove('is-invalid');
    });
  };
  select.addEventListener('change',toggle);
  toggle();
})();


// Password-reset Turnstile: explicit rendering and token handoff.
// Scope: /forgot only. No changes to customer booking/admin behavior.
(function initOtpVerification(){
  const form=document.querySelector('[data-otp-forgot-form]');
  if(!form)return;
  const host=form.querySelector('[data-otp-turnstile]');
  const field=form.querySelector('input[name="cf-turnstile-response"]');
  const status=form.querySelector('[data-otp-status]');
  const submit=form.querySelector('button[type="submit"],button:not([type])');
  if(!host || !field || !status || !submit)return;
  submit.disabled=true;
  const show=(message)=>{status.textContent=message;};
  const clear=()=>{field.value='';submit.disabled=true;};
  const script=document.createElement('script');
  script.src='https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit';
  script.async=true;
  script.onerror=()=>{clear();show('โหลด Cloudflare ไม่สำเร็จ กรุณาตรวจอินเทอร์เน็ตหรือส่วนขยายที่บล็อกเว็บไซต์ แล้วโหลดหน้าใหม่');};
  script.onload=()=>{
    if(!window.turnstile){clear();show('ไม่พบระบบยืนยัน Cloudflare กรุณาโหลดหน้าใหม่');return;}
    try{
      window.turnstile.render(host,{
        sitekey:host.dataset.sitekey,
        action:'rngd-password-reset',
        callback:(token)=>{
          if(!token){clear();show('ยังไม่ได้รับรหัสยืนยันจาก Cloudflare');return;}
          field.value=token;
          submit.disabled=false;
          show('ยืนยันความปลอดภัยสำเร็จ สามารถกดส่งรหัส OTP ได้');
        },
        'expired-callback':()=>{clear();show('การยืนยันหมดอายุ กรุณายืนยัน Cloudflare อีกครั้ง');},
        'timeout-callback':()=>{clear();show('การยืนยันใช้เวลานานเกินไป กรุณาลองอีกครั้ง');},
        'error-callback':(code)=>{
          clear();
          const safeCode=String(code||'').replace(/[^0-9]/g,'').slice(0,6);
          show('Cloudflare ไม่สามารถยืนยันได้'+(safeCode?' (รหัส '+safeCode+')':'')+' กรุณาตรวจการตั้งค่า Turnstile หรือเครือข่าย');
          return true;
        }
      });
    }catch(_){clear();show('เริ่มการตรวจสอบ Cloudflare ไม่สำเร็จ กรุณาตรวจ Site Key และโหลดหน้าใหม่');}
  };
  document.head.append(script);
  form.addEventListener('submit',(event)=>{
    if(!field.value){event.preventDefault();clear();show('กรุณายืนยัน Cloudflare ให้สำเร็จก่อนกดส่งรหัส OTP');}
  });
})();
