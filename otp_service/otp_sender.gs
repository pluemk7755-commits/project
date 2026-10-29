/** Roo Nai Gor Dai OTP delivery: public use, arbitrary registered email recipients.
 * SECURITY: verify Cloudflare Turnstile for every request; require valid Siteverify
 * proof, fixed message template, per-address and global limits.
 * Never deploy with blank TURNSTILE_SECRET. Keep this key in Script Properties only.
 * URL is public; no client-side/shared application password can secure it.
 */
const COOLDOWN_MS = 60 * 1000;
const MAX_PER_RECIPIENT_DAY = 5;
const MAX_TOTAL_DAY = 50; // Personal Google account has finite quotas.

function json_(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj))
    .setMimeType(ContentService.MimeType.JSON);
}

function doGet() {
  return json_({ok:true,service:'Roo Nai Gor Dai OTP'});
}

function senderKey_(email) {
  const dig=Utilities.computeDigest(Utilities.DigestAlgorithm.SHA_256,email);
  return 'recipient_' + dig.map(b=>('0'+(b&255).toString(16)).slice(-2)).join('').substring(0,40);
}

function humanVerified_(token, secret, allowedHosts) {
  if (!token || token.length>2048 || !secret || !allowedHosts.length) return false;
  try {
    const res=UrlFetchApp.fetch('https://challenges.cloudflare.com/turnstile/v0/siteverify', {
      method:'post',
      payload:{secret:secret,response:token},
      muteHttpExceptions:true
    });
    if (res.getResponseCode()!==200) return false;
    const result=JSON.parse(res.getContentText());
    return result.success===true &&
      result.action==='rngd-password-reset' &&
      allowedHosts.indexOf(String(result.hostname||'').toLowerCase())>=0;
  } catch (_) {return false;}
}

function doPost(e) {
  const lock=LockService.getScriptLock();
  if (!lock.tryLock(5000)) return json_({ok:false,error:'BUSY'});
  try {
    const body=e&&e.postData&&e.postData.contents||'';
    if (body.length>4096) return json_({ok:false,error:'INVALID_REQUEST'});
    let request;
    try {request=JSON.parse(body);} catch (_) {return json_({ok:false,error:'INVALID_REQUEST'});}
    const email=String(request.email||'').trim().toLowerCase();
    const code=String(request.code||'');
    const token=String(request.captcha_token||'');
    if (email.length>254 || !/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email) || !/^\d{8}$/.test(code)) {
      return json_({ok:false,error:'INVALID_REQUEST'});
    }
    const props=PropertiesService.getScriptProperties();
    const secret=props.getProperty('TURNSTILE_SECRET')||'';
    const hosts=(props.getProperty('TURNSTILE_HOSTS')||'')
       .toLowerCase().split(',').map(s=>s.trim()).filter(Boolean);
    if (!secret || !hosts.length) return json_({ok:false,error:'NOT_CONFIGURED'});

    const now=Date.now();
    const day=Utilities.formatDate(new Date(now),Session.getScriptTimeZone(),'yyyy-MM-dd');
    const key=senderKey_(email);
    const previous=JSON.parse(props.getProperty(key)||'{}');
    const recipient=previous.day===day?previous:{day:day,count:0,last:0};
    const totalPrevious=JSON.parse(props.getProperty('total_today')||'{}');
    const total=totalPrevious.day===day?totalPrevious:{day:day,count:0};
    if (now-recipient.last<COOLDOWN_MS || recipient.count>=MAX_PER_RECIPIENT_DAY || total.count>=MAX_TOTAL_DAY) {
      return json_({ok:false,error:'RATE_LIMIT'});
    }
    if (!humanVerified_(token,secret,hosts)) return json_({ok:false,error:'HUMAN_VERIFICATION_FAILED'});
    if (MailApp.getRemainingDailyQuota()<=0) return json_({ok:false,error:'QUOTA_REACHED'});

    MailApp.sendEmail({
      to:email,
      subject:'Roo Nai Gor Dai - Password reset',
      body:'รหัสยืนยันของคุณคือ '+code+'\nใช้ได้ 10 นาที และใช้ได้หนึ่งครั้ง',
      name:'Roo Nai Gor Dai'
    });
    props.setProperty(key,JSON.stringify({day:day,count:recipient.count+1,last:now}));
    props.setProperty('total_today',JSON.stringify({day:day,count:total.count+1}));
    return json_({ok:true});
  } catch (_) {return json_({ok:false,error:'SEND_FAILED'});}
  finally {lock.releaseLock();}
}
