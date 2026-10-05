// spinclip_verify.js —— 干净实测：45° 时齿轮是否真的被裁框切（修前 scale1 vs 修后 scale.88）
//
// 做法：把 nav-ind / 其它 .nav / 背景都设成纯色，只留设置齿轮，截"裁框开/关"两版，
//       量墨迹最远点离裁框中心的距离。hidden < visible ⇒ 被切。
const { spawn } = require('child_process');
const http = require('http'); const fs = require('fs'); const path = require('path');
const WebSocket = globalThis.WebSocket;
const ROOT='<REPO>\\gui'; const PORT=8799; const CDP=9349;
const CHROME='chrome';
const UDD='<REPO>\\output\\.tmp\\spinclipv-profile';
const OUT='<REPO>\\output\\.tmp\\spinclipv';
const MIME={'.html':'text/html','.js':'text/javascript','.css':'text/css','.svg':'image/svg+xml','.png':'image/png'};
const WIN=44, SCALE=8;
const sleep=ms=>new Promise(r=>setTimeout(r,ms));
function serve(){return new Promise(res=>{const s=http.createServer((q,s2)=>{let p=q.url.split('?')[0];if(p==='/')p='/index.html';fs.readFile(path.join(ROOT,p),(e,b)=>{if(e){s2.writeHead(404);s2.end();return;}s2.writeHead(200,{'Content-Type':MIME[path.extname(p)]||'application/octet-stream'});s2.end(b);});});s.listen(PORT,()=>res(s));});}
const get=pp=>new Promise((res,rej)=>{http.get({host:'127.0.0.1',port:CDP,path:pp},r=>{let b='';r.on('data',c=>b+=c);r.on('end',()=>res(b));}).on('error',rej);});
async function main(){
  fs.rmSync(OUT,{recursive:true,force:true}); fs.mkdirSync(OUT,{recursive:true});
  const srv=await serve();
  const chrome=spawn(CHROME,['--headless=new','--no-sandbox','--disable-gpu','--force-device-scale-factor=1.5','--remote-debugging-port='+CDP,'--user-data-dir='+UDD,'about:blank'],{stdio:'ignore'});
  await sleep(1300);
  const t=JSON.parse(await get('/json/list')); const page=t.find(x=>x.type==='page')||t[0];
  const ws=new WebSocket(page.webSocketDebuggerUrl); let id=0; const pend={};
  const send=(m,p)=>new Promise(r=>{const i=++id;pend[i]=r;ws.send(JSON.stringify({id:i,method:m,params:p||{}}));});
  await new Promise(r=>ws.addEventListener('open',()=>r()));
  ws.addEventListener('message',d=>{const m=JSON.parse(d.data);if(m.id&&pend[m.id]){pend[m.id](m.result);delete pend[m.id];}});
  const ev=async(ex)=>{const r=await send('Runtime.evaluate',{expression:ex,returnByValue:true,awaitPromise:true});if(r.exceptionDetails)throw new Error(JSON.stringify(r.exceptionDetails));return r.result.value;};
  await send('Page.enable');await send('Runtime.enable');
  await send('Page.navigate',{url:'http://127.0.0.1:'+PORT+'/index.html'});
  await sleep(1500);
  const meta=await ev(`(function(){return new Promise(function(res){
    var tries=0;(function poll(){
      try{
        var sb=document.getElementById('sidebar');
        var nav=sb.querySelector('.nav[data-page="settings"]');
        var clip=nav.querySelector('.ico-clip'); var ico=nav.querySelector('.ico');
        if(!clip||!ico){if(++tries<80)return setTimeout(poll,50);return res({err:'no icon'});}
        var ind=document.getElementById('nav-ind'); if(ind) ind.style.display='none';
        Array.prototype.forEach.call(sb.querySelectorAll('.nav'),function(n){if(n!==nav)n.style.visibility='hidden';});
        // 白底 + 红齿轮：红/白差异巨大，墨迹必能检出（不依赖主题深浅）
        document.body.style.background='#ffffff';
        sb.style.background='#ffffff'; nav.style.background='#ffffff';
        clip.style.background='#ffffff';
        ico.style.color='#ff0000';
        var cb=clip.getBoundingClientRect();
        res({dpr:devicePixelRatio, box:{x:cb.left,y:cb.top,w:cb.width,h:cb.height}});
      }catch(e){res({err:String(e)});}
    })();
  });})()`);
  if(meta.err){console.error('FAILED',meta.err);ws.close();chrome.kill('SIGKILL');srv.close();return;}
  const clip={x:meta.box.x+meta.box.w/2-WIN/2,y:meta.box.y+meta.box.h/2-WIN/2,width:WIN,height:WIN,scale:SCALE};
  const shoot=async(name,scale)=>{
    await send('Runtime.evaluate',{expression:`(function(){
      var nav=document.querySelector('#sidebar .nav[data-page="settings"]');
      var clipEl=nav.querySelector('.ico-clip'); var ico=nav.querySelector('.ico');
      clipEl.style.overflow='${name.endsWith('hidden')?'hidden':'visible'}';
      ico.style.transition='none'; ico.style.animation='none';
      ico.style.transform='rotate(45deg) scale(${scale})';
      void ico.offsetWidth;
    })()`,returnByValue:true});
    const r=await send('Page.captureScreenshot',{format:'png',clip});
    fs.writeFileSync(path.join(OUT,name+'.png'),Buffer.from(r.data,'base64'));
  };
  // 修前：scale(1)；修后：scale(.88)
  await shoot('s1_hidden',1); await shoot('s1_visible',1);
  await shoot('s88_hidden',0.88); await shoot('s88_visible',0.88);
  fs.writeFileSync(path.join(OUT,'meta.json'),JSON.stringify({clip,meta}));
  ws.close();chrome.kill('SIGKILL');srv.close();
  console.log('DONE ⇒ verify_clip_final.py');
}
main().catch(e=>{console.error(e);process.exit(1);});
