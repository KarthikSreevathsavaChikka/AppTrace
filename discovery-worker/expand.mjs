import {checkFinished} from './node_modules/google-play-scraper/lib/utils/processPages.js';
let input='';for await(const chunk of process.stdin)input+=chunk;
try{
 const cfg=JSON.parse(input), url=new URL(cfg.href,'https://play.google.com');
 if(url.origin!=='https://play.google.com'||!['/store/apps/dev','/store/apps/developer','/store/apps/collection/cluster'].includes(url.pathname))throw Error('Unsupported section URL');
 const limit=Math.max(1,Math.min(200,Number(cfg.limit)||30));
 url.searchParams.set('hl',cfg.language);url.searchParams.set('gl',cfg.country);
 const response=await fetch(url,{signal:AbortSignal.timeout(30000)});if(!response.ok)throw Error(`Section HTTP ${response.status}`);
 const html=await response.text(), datasets=[];
 for(const match of html.matchAll(/AF_initDataCallback\([\s\S]*?data:([\s\S]*?),\s*sideChannel:\s*\{\}\}\);/g)){
  try{datasets.push(JSON.parse(match[1]));}catch{}
 }
 let entries,token;
 for(const data of datasets){for(const pos of [21,22]){const section=data?.[0]?.[1]?.[0]?.[pos];const candidate=section?.[0];if(Array.isArray(candidate)&&candidate.length){const first=candidate[0];if(typeof first?.[0]?.[0]==='string'||typeof first?.[0]?.[0]?.[0]==='string'){entries=candidate;token=section?.[1]?.[3]?.[1];break;}}}if(entries)break;}
 if(!entries)throw Error('Section app structure unavailable; pagination could not be verified');
 const items=entries.map(raw=>{const a=typeof raw?.[0]?.[0]==='string'?raw:raw[0];return {appId:a?.[0]?.[0],title:a?.[3],icon:a?.[1]?.[3]?.[2],developer:a?.[14]};}).filter(a=>typeof a.appId==='string'&&a.appId.includes('.'));
 if(!items.length)throw Error('No valid app records in section');
 const results=await checkFinished({num:limit,lang:cfg.language,country:cfg.country,numberOfApps:Math.min(limit,100),fullDetail:false,throttle:1,requestOptions:{timeout:30000}},items,token);
 process.stdout.write(JSON.stringify({apps:[...new Map(results.map(a=>[a.appId,a])).values()],limit_reached:results.length>=limit}));
}catch(error){console.error(error.message);process.exitCode=1;}
