import test from "node:test";
import assert from "node:assert/strict";
import * as fs from "node:fs/promises";
import {watch} from "node:fs";
import {tmpdir} from "node:os";
import {dirname} from "node:path";
import {execFile} from "node:child_process";
import {fileURLToPath} from "node:url";
import {openSession} from "../skills/codex-pro-dispatch/scripts/parked-socket.mjs";

const root=fileURLToPath(new URL("../",import.meta.url));
const scripts=root+"skills/codex-pro-dispatch/scripts/";
const pause=ms=>new Promise(r=>setTimeout(r,ms));
async function missing(path){
await assert.rejects(fs.lstat(path),e=>e.code==="ENOENT");
}
function appeared(path){
return new Promise((resolve,reject)=>{
let done=false;
const w=watch(dirname(path),()=>void check());
const timer=setTimeout(()=>end(Error("Missing fixture event")),8000);
function end(e,v){
if(done)return;done=true;clearTimeout(timer);w.close();e?reject(e):resolve(v);
}
async function check(){
try{end(null,JSON.parse(await fs.readFile(path,"utf8")));}
catch(e){if(e.code!=="ENOENT"&&!(e instanceof SyntaxError))end(e);}
}
w.on("error",e=>end(e));void check();
});
}
async function fixture(t,idleMs=5000,resident=false){
const d=await fs.realpath(await fs.mkdtemp(tmpdir()+"/pro-rendezvous-unit-"));
const session=d+"/session",home=d+"/authority",helper=root+"bin/pro-dispatch";
const env={...process.env,CODEX_PRO_DISPATCH_HOME:home};
const outstanding=new Set();
function run(file,args){
const p=new Promise((resolve,reject)=>{
const child=execFile(file,args,{env,timeout:30000,maxBuffer:8388608},(error,out,err)=>{
try{resolve({code:error?error.code:0,value:JSON.parse(error?err:out)});}
catch(e){reject(Error(err||out||String(e)));}
});
child.stdin?.end();
});
outstanding.add(p);
p.then(()=>outstanding.delete(p),()=>outstanding.delete(p));
return p;
}
async function cli(args){
const r=await run("python3",[helper,...args]);
assert.equal(r.code,0,JSON.stringify(r.value));return r.value;
}
await cli(["worker","set","--conversation-id","fixture-pro",
"--confirm-pro","--native-controls-confirmed"]);
await fs.mkdir(session,{mode:448});
const socket=await openSession(session,{
helper,configDir:home+"/config",stateDir:home+"/state",
worker:"fixture-pro",parent:"fixture-parent",
leaseMs:resident?null:60000,idleMs,replyMs:10000,...(resident?{resident:true}:{})
});
const prompt=d+"/prompt.txt";
await fs.writeFile(prompt,"Review exact bytes: café.\n",{mode:384});
const activate=(...args)=>run(process.execPath,[scripts+"parked-activation.mjs",...args]);
// A resident client publishes only toward a live resident-next waiter; this
// fixture drives command-ready directly, so it stands in as that waiter.
const beats=new Set();
async function waiting(ordinal){
if(!resident)return;
const marker=session+"/waiting-"+ordinal+"."+socket.config.sessionId;
try{await fs.mkdir(marker,{mode:448});}
catch(e){if(e.code!=="EEXIST")throw e;}
beats.add(setInterval(()=>fs.utimes(marker,new Date(),new Date()).catch(()=>{}),1000));
}
const start=async(ordinal,rid,file=prompt)=>{await waiting(ordinal);return activate(
"rendezvous",session,String(ordinal),rid,file,"fixture-client");};
const retry=async(ordinal,rid,attempt)=>{await waiting(ordinal);return activate(
"rendezvous-retry",session,String(ordinal),rid,attempt);};
const gate=(ordinal,rid,attempt,preload)=>run(process.execPath,[
...(preload?["--require",preload]:[]),scripts+"parked-activation.mjs",
"command-ready",session,String(ordinal),rid,...(attempt===undefined?[]:[attempt])
]);
const client=(operation,rid)=>run(process.execPath,[
scripts+"parked-client.mjs",session,operation,rid,
...(operation==="submit"?[prompt,"fixture-client"]:[])
]);
async function complete(delivery){
const rid=delivery.requestId;
const claim=await cli(["queue","claim","--request-id",rid,
"--parent-task-id","fixture-parent","--native-controls-confirmed"]);
await cli(["arm",rid]);
const raw=JSON.stringify({
schemaVersion:1,thread:{id:"fixture-pro",kind:"chatgpt",status:{type:"idle"}},
turns:[{id:"turn-"+rid,items:[
{id:"turn-"+rid,type:"userMessage",content:[{type:"text",text:claim.wrapped_prompt}]},
{id:"answer-"+rid,type:"agentMessage",text:
"[CODEX_PRO_DISPATCH_RESULT assignment_id="+rid+"]\nfixture answer\n"+
"[CODEX_PRO_DISPATCH_END assignment_id="+rid+"]"}
]}]});
const evidence=d+"/history-"+rid+".json";
await fs.writeFile(evidence,raw,{mode:384});
const answer=await cli(["queue","observe",rid,"--parent-task-id","fixture-parent",
"--native-controls-confirmed","--native-read-file",evidence]);
await socket.finish(delivery.callId,answer);
return answer;
}
t.after(async()=>{
for(const beat of beats)clearInterval(beat);
await socket.close("unit_finished");
await Promise.allSettled([...outstanding]);
await fs.rm(d,{recursive:true});
});
return {d,session,prompt,socket,cli,start,retry,gate,client,complete};
}

test("slow command startup precedes receive; same session handles A and B",async t=>{
const f=await fixture(t);
const gateA=f.gate(1,"request-A");
// Real delay exceeds this fixture's idle window. Native receive has not begun.
await pause(5100);
await missing(f.session+"/ready-1.json");
await missing(f.session+"/transport-audit.json");
assert.deepEqual((await f.cli(["queue","status"])).requests,[]);
const a=f.start(1,"request-A");
const ready=await gateA;
assert.equal(ready.code,0);assert.equal(ready.value.commandReady,true);
await missing(f.session+"/ready-1.json");
const first=await f.socket.receive();
assert.equal(first.requestId,"request-A");
await f.complete(first);
const result=await a;
assert.equal(result.code,0);
assert.equal(result.value.answer.payload,"fixture answer");
assert.deepEqual((await f.client("collect","request-A")).value.answer,result.value.answer);
assert.deepEqual((await f.client("submit","request-A")).value.answer,result.value.answer);
assert.equal((await f.cli(["status","request-A"])).assignment.submission_count,1);

const b=f.start(2,"request-B");
await appeared(f.session+"/command-2.json");
// The parent's initial read also handles an already-published command signal.
assert.equal((await f.gate(2,"request-B")).code,0);
const second=await f.socket.receive();
assert.equal(second.requestId,"request-B");
await f.complete(second);
assert.equal((await b).value.answer.payload,"fixture answer");
assert.equal((await f.cli(["status","request-B"])).assignment.submission_count,1);
await f.socket.close("unit_complete");
const audit=JSON.parse(await fs.readFile(f.session+"/transport-audit.json","utf8"));
assert.deepEqual(audit.events.filter(e=>e.name==="accepted").map(e=>e.requestId),
["request-A","request-B"]);
});

for(const resident of [false,true])test(`oversized prompts do not consume a handoff; same ordinal still works (resident=${resident})`,async t=>{
const f=await fixture(t,5000,resident);
for(const body of ["x".repeat(20000),"😀".repeat(10000)," \n\t"]){
await fs.writeFile(f.prompt,body,{mode:384});
const result=await f.start(1,"request-A");
assert.notEqual(result.code,0);
assert.match(result.value.error,/native read limit|Prompt is empty/);
for(const name of ["command-1","command-1.json","command-observed-1.json",
"ready-1.json","transport-audit.json"])await missing(f.session+"/"+name);
assert.deepEqual((await f.cli(["queue","status"])).requests,[]);
}
await fs.writeFile(f.prompt,"valid request",{mode:384});
const result=f.start(1,"request-A");
assert.equal((await f.gate(1,"request-A")).code,0);
const delivery=await f.socket.receive();
await f.complete(delivery);
assert.equal((await result).code,0);
assert.equal((await f.cli(["status","request-A"])).assignment.submission_count,1);
});

test("unsafe prompt fails before command readiness, queue creation or receive",async t=>{
const f=await fixture(t);
const bad=f.d+"/public";await fs.mkdir(bad,{mode:493});
await fs.writeFile(bad+"/prompt.txt","private file, unsuitable parent",{mode:384});
const alias=f.d+"/alias.txt";await fs.symlink(f.prompt,alias);
const invalid=f.d+"/invalid.txt";
await fs.writeFile(invalid,Buffer.from([255]),{mode:384});
for(const path of [bad+"/prompt.txt",alias,invalid]){
const r=await f.start(1,"request-A",path);
assert.notEqual(r.code,0);
await missing(f.session+"/command-1.json");
await missing(f.session+"/command-1");
await missing(f.session+"/ready-1.json");
}
assert.deepEqual((await f.cli(["queue","status"])).requests,[]);
assert.equal((await f.cli(["status"])).active_assignment,null);
});

test("duplicate rendezvous and duplicate gate cannot authorize another receive",async t=>{
const f=await fixture(t);
const a=f.start(1,"request-A");
await appeared(f.session+"/command-1.json");
assert.notEqual((await f.start(1,"request-A")).code,0);
assert.equal((await f.gate(1,"request-A")).code,0);
assert.notEqual((await f.gate(1,"request-A")).code,0);
const delivery=await f.socket.receive();
await f.complete(delivery);
assert.equal((await a).code,0);
assert.equal((await f.cli(["status","request-A"])).assignment.submission_count,1);
await missing(f.session+"/ready-2.json");
});

test("stale native readiness and wrong-request command gate are rejected",async t=>{
const f=await fixture(t);
await fs.writeFile(f.session+"/ready-1.json",JSON.stringify({
sessionId:f.socket.config.sessionId,ordinal:1,at:Date.now()
}),{mode:384});
assert.notEqual((await f.start(1,"request-A")).code,0);
await missing(f.session+"/command-1.json");
const a=f.start(2,"request-B");
await appeared(f.session+"/command-2.json");
assert.notEqual((await f.gate(2,"wrong-request")).code,0);
await missing(f.session+"/command-observed-2.json");
await f.socket.close("unit_stop_without_admission");
assert.notEqual((await a).code,0);
assert.deepEqual((await f.cli(["queue","status"])).requests,[]);
});

test("rendezvous timeout neither submits nor starts or rearms native receive",async t=>{
const f=await fixture(t,1200);
const result=await f.start(1,"request-A");
assert.notEqual(result.code,0);
await missing(f.session+"/ready-1.json");
await missing(f.session+"/ready-2.json");
assert.deepEqual((await f.cli(["queue","status"])).requests,[]);
assert.equal((await f.cli(["status"])).active_assignment,null);
assert.notEqual((await f.start(1,"request-A")).code,0);
await f.socket.close("unit_no_receive");
const audit=JSON.parse(await fs.readFile(f.session+"/transport-audit.json","utf8"));
assert.deepEqual(audit.events,[]);
});

const attemptA="a".repeat(32),attemptB="b".repeat(32);
const commandFile=(f,n,a)=>f.session+"/command-"+n+
(a===undefined?"":"-retry-"+a)+".json";
async function expire(f,n,id,a){
const p=a===undefined?f.start(n,id):f.retry(n,id,a);
const v=await appeared(commandFile(f,n,a)),r=await p;
assert.notEqual(r.code,0,JSON.stringify(r.value));
assert(Date.now()>=v.deadlineAt);
await missing(f.session+"/ready-"+n+".json");
await missing(f.session+"/command-observed-"+n+".json");
return v;
}
async function finishRendezvous(f,n,id,p,a){
await appeared(commandFile(f,n,a));
const g=await f.gate(n,id,a);
assert.equal(g.code,0,JSON.stringify(g.value));
const delivery=await f.socket.receive();
assert.equal(delivery?.requestId,id);
await f.complete(delivery);
const r=await p;
assert.equal(r.code,0,JSON.stringify(r.value));
assert.equal(r.value.answer.payload,"fixture answer");
assert.equal((await f.cli(["status",id])).assignment.submission_count,1);
}
async function assertDeliveries(f,ids){
await f.socket.close("unit_delivery_counts");
const audit=JSON.parse(await fs.readFile(f.session+"/transport-audit.json","utf8"));
assert.deepEqual(audit.events.map(e=>[e.name,e.requestId]),
ids.flatMap(id=>[["accepted",id],["finished",id]]));
}

test("A, expired ordinal 2, same-request retry, then B at ordinal 3",async t=>{
const f=await fixture(t,8000);
await finishRendezvous(f,1,"request-A",f.start(1,"request-A"));
const original=await expire(f,2,"request-R");
const names=["command-2.json","command-2/prompt.txt"],before=[];
for(const n of names)before.push(await fs.readFile(f.session+"/"+n));
assert(!(await f.cli(["queue","status"])).requests.some(r=>r.request_id==="request-R"));
assert.notEqual((await f.start(2,"request-R")).code,0);
// Retry must use the private original, not this subsequently changed input.
await fs.writeFile(f.prompt,"Changed caller input\n");
const pending=f.retry(2,"request-R",attemptA);
const record=await appeared(commandFile(f,2,attemptA));
for(const k of ["sessionId","ordinal","requestId","clientSessionId","promptSha256"])
assert.equal(record[k],original[k]);
assert.equal(record.nonce,attemptA);
assert.deepEqual(await fs.readFile(f.session+"/command-2-retry-"+attemptA+"/prompt.txt"),before[1]);
await finishRendezvous(f,2,"request-R",pending,attemptA);
await finishRendezvous(f,3,"request-B",f.start(3,"request-B"));
for(let i=0;i<names.length;i++)
assert.deepEqual(await fs.readFile(f.session+"/"+names[i]),before[i]);
await missing(f.session+"/ready-4.json");
await assertDeliveries(f,["request-A","request-R","request-B"]);
});

test("same and different retry IDs cannot produce two admissions",async t=>{
const f=await fixture(t,8000);
await expire(f,1,"request-R");
const copies=[f.retry(1,"request-R",attemptA),f.retry(1,"request-R",attemptA)];
await appeared(commandFile(f,1,attemptA));
const originalBytes=await fs.readFile(commandFile(f,1,attemptA));
const lost=await Promise.race(copies.map((p,i)=>p.then(r=>({i,r}))));
assert.notEqual(lost.r.code,0);
assert.match(lost.r.value.error,/EEXIST|Existing rendezvous artifact/);
const first=copies[1-lost.i];
assert.deepEqual(await fs.readFile(commandFile(f,1,attemptA)),originalBytes);
const second=f.retry(1,"request-R",attemptB);
await appeared(commandFile(f,1,attemptB));
const attempts=[attemptA,attemptB];
const gates=await Promise.all(attempts.map(a=>f.gate(1,"request-R",a)));
assert.equal(gates.filter(g=>g.code===0).length,1,JSON.stringify(gates));
const winner=gates.findIndex(g=>g.code===0);
assert.deepEqual(await fs.readFile(f.session+"/command-observed-1.json"),
await fs.readFile(commandFile(f,1,attempts[winner])));
const delivery=await f.socket.receive();
assert.equal(delivery?.requestId,"request-R");
await f.complete(delivery);
const clients=await Promise.all([first,second]);
assert.equal(clients[winner].code,0,JSON.stringify(clients[winner]));
assert.notEqual(clients[1-winner].code,0);
assert.match(clients[1-winner].value.error,/Command gate differs/);
for(const a of attempts){
assert.notEqual((await f.gate(1,"request-R",a)).code,0);
assert.notEqual((await f.retry(1,"request-R",a)).code,0);
}
assert.equal((await f.cli(["status","request-R"])).assignment.submission_count,1);
await missing(f.session+"/ready-2.json");
await assertDeliveries(f,["request-R"]);
});

test("another unobserved timeout preserves both attempts and allows a fresh ID",async t=>{
const f=await fixture(t,8000);
await expire(f,1,"request-R");
await expire(f,1,"request-R",attemptA);
const paths=[commandFile(f,1),commandFile(f,1,attemptA),
f.session+"/command-1/prompt.txt",f.session+"/command-1-retry-"+attemptA+"/prompt.txt"];
const before=await Promise.all(paths.map(p=>fs.readFile(p)));
assert.deepEqual((await f.cli(["queue","status"])).requests,[]);
assert.notEqual((await f.retry(1,"request-R",attemptA)).code,0);
await finishRendezvous(f,1,"request-R",f.retry(1,"request-R",attemptB),attemptB);
assert.deepEqual(await Promise.all(paths.map(p=>fs.readFile(p))),before);
await assertDeliveries(f,["request-R"]);
});

test("retry refuses unsafe or mismatched pre-admission state",async t=>{
for(const kind of ["observed","ready","lease","queued-resume","request","session","nonce","prompt"])
await t.test(kind,async t=>{
const f=await fixture(t,1200),v=await expire(f,1,"request-R");
let id="request-R",attempt=attemptA;
const config={...f.socket.config};
if(kind==="observed")await fs.writeFile(f.session+"/command-observed-1.json",
JSON.stringify(v),{mode:384});
if(kind==="ready")await fs.writeFile(f.session+"/ready-1.json",
JSON.stringify({sessionId:v.sessionId,ordinal:1,at:Date.now()}),{mode:384});
if(kind==="lease")config.expiresAt=Date.now()-1;
if(kind==="queued-resume")config.queuedResume={
sessionId:"e".repeat(32),requestId:id,callId:"c".repeat(32),
fingerprint:"f".repeat(64),clientSessionId:v.clientSessionId,
nonce:v.nonce,promptSha256:v.promptSha256};
if(["lease","queued-resume"].includes(kind))
await fs.writeFile(f.session+"/session.json",JSON.stringify(config));
if(kind==="request")id="wrong-request";
if(kind==="session"){
v.sessionId="0".repeat(32);
await fs.writeFile(commandFile(f,1),JSON.stringify(v));
}
if(kind==="nonce")attempt=v.nonce;
if(kind==="prompt")await fs.appendFile(f.session+"/command-1/prompt.txt","altered");
const r=await f.retry(1,id,attempt);
assert.notEqual(r.code,0,JSON.stringify(r.value));
if(kind!=="prompt")assert.notEqual((await f.gate(1,id,attempt)).code,0);
await missing(commandFile(f,1,attempt));
await missing(f.session+"/command-1-retry-"+attempt);
assert.deepEqual((await f.cli(["queue","status"])).requests,[]);
assert.equal((await f.cli(["status"])).active_assignment,null);
await missing(f.session+"/ready-2.json");
});
});

test("late original gate consumes its claim but cannot authorize a receive",async t=>{
const f=await fixture(t,8000),signal=f.d+"/paused.json",release=f.d+"/release";
const hook=f.d+"/gate-pause.cjs",observed=f.session+"/command-observed-1.json";
// Child-only barrier after the pre-write deadline check, before exclusive write.
await fs.writeFile(hook,`
const fs=require("node:fs/promises"),{existsSync}=require("node:fs");
const write=fs.writeFile;
fs.writeFile=async function(path,...args){
if(path===${JSON.stringify(observed)}){
await write(${JSON.stringify(signal)},"{}");
const limit=Date.now()+20000;
while(!existsSync(${JSON.stringify(release)})){
if(Date.now()>limit)throw Error("Fixture barrier timed out");
await new Promise(r=>setTimeout(r,10));
}
}
return write(path,...args);
};
require("node:module").syncBuiltinESMExports();
`);
const client=f.start(1,"request-R");
const original=await appeared(commandFile(f,1));
const oldGate=f.gate(1,"request-R",undefined,hook);
try{
await appeared(signal);
assert.notEqual((await client).code,0);
await missing(observed);
const retry=f.retry(1,"request-R",attemptA);
await appeared(commandFile(f,1,attemptA));
await fs.writeFile(release,"");
const g=await oldGate;
assert.notEqual(g.code,0);
assert.match(g.value.error,/expired after claim/);
assert.deepEqual(await appeared(observed),original);
assert.notEqual((await f.gate(1,"request-R",attemptA)).code,0);
await missing(f.session+"/ready-1.json");
await f.socket.close("unit_late_gate");
assert.notEqual((await retry).code,0);
assert.deepEqual((await f.cli(["queue","status"])).requests,[]);
}finally{
await fs.writeFile(release,"");
await oldGate;
}
});
