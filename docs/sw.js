const C='mb-gh-v1';
self.addEventListener('install',e=>{e.waitUntil(caches.open(C).then(c=>c.add('./')));self.skipWaiting()});
self.addEventListener('activate',e=>e.waitUntil(clients.claim()));
self.addEventListener('fetch',e=>{
 const r=e.request,u=new URL(r.url);
 if(r.method!=='GET'||u.pathname.endsWith('.pdf'))return;
 e.respondWith(fetch(r).then(res=>{
  if(res.ok||res.type==='opaque'){const cp=res.clone();caches.open(C).then(c=>c.put(r,cp))}
  return res}).catch(()=>caches.match(r)))});
