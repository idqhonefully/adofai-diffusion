// gearbox.js —— 在浏览器内用 canvas 单独画设置齿轮字形，量真实墨迹包围盒
// 目的：拿到齿轮在 em 盒里的真实墨迹半对角，判断"旋转 45° 时对角齿是否越出 22px 裁框"。
//       不受 DOM 布局 / 裁框 / nav-ind 污染，比截图测量可靠。
const { spawn } = require('child_process');
const http = require('http');
const fs = require('fs');
const path = require('path');
const WebSocket = globalThis.WebSocket;
const ROOT = '<REPO>\\gui';
const PORT = 8798; const CDP = 9348;
const CHROME = 'chrome';
const UDD = '<REPO>\\output\\.tmp\\gearbox-profile';
const sleep = (ms) => new Promise(r => setTimeout(r, ms));
function serve(){return new Promise(res=>{const s=http.createServer((q,s2)=>{let p=q.url.split('?')[0];if(p==='/')p='/index.html';fs.readFile(path.join(ROOT,p),(e,b)=>{if(e){s2.writeHead(404);s2.end();return;}s2.writeHead(200,{'Content-Type':'text/html'});s2.end(b);});});s.listen(PORT,()=>res(s));});}
const get=(pp)=>new Promise((res,rej)=>{http.get({host:'127.0.0.1',port:CDP,path:pp},r=>{let b='';r.on('data',c=>b+=c);r.on('end',()=>res(b));}).on('error',rej);});
async function main(){
  const srv=await serve();
  const chrome=spawn(CHROME,['--headless=new','--no-sandbox','--disable-gpu','--force-device-scale-factor=1','--remote-debugging-port='+CDP,'--user-data-dir='+UDD,'about:blank'],{stdio:'ignore'});
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
  const out=await ev(`(async function(){
    return await new Promise(async function(res){
      var tries=0;
      async function poll(){
        try{
          var nav=document.querySelector('#sidebar .nav[data-page="settings"]');
          var g=nav&&nav.querySelector('.ico-glyph');
          if(!g){if(++tries<80)return setTimeout(poll,50);return res({err:'no glyph'});}
          var ch=g.textContent;
          var FAM="'Segoe Fluent Icons','Segoe MDL2 Assets'";
          try{ await document.fonts.load('16px '+FAM); await document.fonts.load('20px '+FAM); await document.fonts.ready; }catch(e){}
          var rows=[];
          [16,18,20].forEach(function(fs){
            var S=96, cv=document.createElement('canvas'); cv.width=S; cv.height=S;
            var ctx=cv.getContext('2d');
            ctx.clearRect(0,0,S,S);
            ctx.fillStyle='#000'; ctx.textBaseline='middle'; ctx.textAlign='center';
            ctx.font=fs+'px '+FAM;
            ctx.fillText(ch, S/2, S/2);
            var img=ctx.getImageData(0,0,S,S).data;
            var minx=S,miny=S,maxx=0,maxy=0,cnt=0;
            var pts=[];
            for(var y=0;y<S;y++)for(var x=0;x<S;x++){
              var a=img[(y*S+x)*4+3]; if(a>40){cnt++; if(x<minx)minx=x;if(x>maxx)maxx=x;if(y<miny)miny=y;if(y>maxy)maxy=y; pts.push([x,y]);}
            }
            var w=maxx-minx, h=maxy-miny;
            var cx=(minx+maxx)/2 - S/2, cy=(miny+maxy)/2 - S/2;
            var maxR=0;
            for(var k=0;k<pts.length;k++){var dx=pts[k][0]-(S/2+cx), dy=pts[k][1]-(S/2+cy); var r=Math.sqrt(dx*dx+dy*dy); if(r>maxR)maxR=r;}
            var halfDiag=0.5*Math.sqrt(w*w+h*h);
            var pxPerCss=S/fs;
            rows.push({fontSizePx:fs, inkCount:cnt, bboxW:w, bboxH:h,
              inkCenterOffsetCss:{x:+(cx/pxPerCss).toFixed(2), y:+(cy/pxPerCss).toFixed(2)},
              inkHalfDiagCss:+(halfDiag/pxPerCss).toFixed(2),
              inkMaxRadiusCss:+(maxR/pxPerCss).toFixed(2),
              inkCenterDistCss:+(Math.sqrt(cx*cx+cy*cy)/pxPerCss).toFixed(2)});
          });
          res({char:ch, fontFamily:FAM, rows:rows});
        }catch(e){ res({err:String(e), stack:String(e&&e.stack)}); }
      }
      poll();
    });
  })()`);
  console.log(JSON.stringify(out,null,2));
  ws.close();chrome.kill('SIGKILL');srv.close();
}
main().catch(e=>{console.error(e);process.exit(1);});
