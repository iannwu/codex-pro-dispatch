import test from "node:test";
import assert from "node:assert/strict";
import * as fs from "node:fs/promises";
import {tmpdir} from "node:os";
import {execFile} from "node:child_process";
import {EventEmitter} from "node:events";
import {fileURLToPath} from "node:url";
import nativeFs from "node:fs/promises";
import {syncBuiltinESMExports} from "node:module";

const root=fileURLToPath(new URL("../",import.meta.url));
const scripts=root+"skills/codex-pro-dispatch/scripts/",author=scripts+"parked-activation.mjs";
const P="resident-parent",W="resident-pro",J=JSON.stringify;
const AF=Object.getPrototypeOf(async function(){}).constructor;
const pause=ms=>new Promise(r=>setTimeout(r,ms));
async function waitForFile(p){
for(let i=0;i<500;i++){
try{await fs.lstat(p);return;}catch(e){if(e.code!=="ENOENT")throw e;}
await new Promise(r=>setTimeout(r,10));
}
throw Error("Missing fixture file: "+p);
}
const mcp=v=>({content:[{type:"text",text:J(v)}]});
async function missing(p){await assert.rejects(fs.lstat(p),e=>e.code==="ENOENT");}

// Real CLI, queue, activation and transport. Synthetic native responses.
async function fixture(t,mode="ok"){
const base=process.platform==="darwin"?"/private/tmp":tmpdir();
const d=await fs.realpath(await fs.mkdtemp(base+"/resident-unit-"));
const env={...process.env,CODEX_PRO_DISPATCH_HOME:d+"/authority"};
const priorHome=process.env.CODEX_PRO_DISPATCH_HOME;
process.env.CODEX_PRO_DISPATCH_HOME=env.CODEX_PRO_DISPATCH_HOME;
const g={},meta={threadId:P,"x-codex-turn-metadata":{turn_id:"unit-turn"}};
const s={waits:0,reads:0,repls:0,sends:[],tick:0},events=new EventEmitter();
const children=new Set(),jobs=new Set(),sessions=new Map(),out=[];
let serial=0,last=null;
function track(p){
jobs.add(p);p.then(()=>jobs.delete(p),()=>jobs.delete(p));return p;
}
function run(file,args){
return track(new Promise(resolve=>{
const child=execFile(file,args,{env,timeout:30000,maxBuffer:8388608},(error,out,err)=>{
children.delete(child);
resolve({exit_code:error?1:0,output:error?(err||String(error)):out});
});
children.add(child);child.stdin?.end();
}));
}
async function json(file,args){
const r=await run(file,args);assert.equal(r.exit_code,0,r.output);return JSON.parse(r.output);
}
const cli=args=>json("python3",[root+"bin/pro-dispatch",...args]);
const activate=(args,preload=[])=>json(process.execPath,[...preload,author,...args]);
t.after(async()=>{
try{
await (await import(author)).stopResidentAdmission(g.parkedResident??{}).catch(e=>{
if(mode!=="beginlostwrite")throw e;assert.equal(e.code,"EEXIST");
});
await g.parkedSocket?.close("unit_cleanup").catch(e=>{if(mode!=="recoveryaudit")throw e;assert.equal(e.code,"EEXIST");});
for(const child of children)child.kill("SIGTERM");
await Promise.allSettled([...jobs]);
}finally{
if(priorHome===undefined)delete process.env.CODEX_PRO_DISPATCH_HOME;
else process.env.CODEX_PRO_DISPATCH_HOME=priorHome;
await fs.rm(d,{recursive:true,force:true});}
});
function until(fn){
return new Promise((resolve,reject)=>{
const timer=setTimeout(()=>end(Error("Missing resident test event")),15000);
function end(e){clearTimeout(timer);events.off("change",check);e?reject(e):resolve();}
function check(){if(fn())end();}
events.on("change",check);check();
});
}
await cli(["worker","set","--conversation-id",W,"--confirm-pro","--native-controls-confirmed"]);
const paths=(await cli(["status","--current"])).paths;
if(mode==="poolturn"){
const cryptoLocal=await import("node:crypto");
const hash=raw=>cryptoLocal.createHash("sha256").update(raw).digest("hex");
const evidence=J({kind:"legacy_quiescence",...paths,implementation:"unit",observations:"isolated",authorization:"test",physical_quiescence:true});
await fs.writeFile(d+"/pool-evidence.json",evidence,{mode:384});
await fs.writeFile(d+"/workers.json",J([W,"resident-pro-b"].map((id,i)=>({slot:"slot-"+i,conversation_id:id,label:id,model_confirmation:"user-confirmed-worker",configured_at:"fixture"}))),{mode:384});
await cli(["worker-pool","activate","--workers-file",d+"/workers.json","--expected-legacy-sha256",hash(await fs.readFile(paths.config_dir+"/worker.json")),"--evidence-file",d+"/pool-evidence.json","--evidence-sha256",hash(evidence),"--native-controls-confirmed"]);
}
const qualification=J({kind:"fresh_deployment",parent:P,worker:W,...paths,
implementation:"unit-fixture",observations:"New isolated test authority, no native host",authorization:"Test harness only"});
await fs.writeFile(d+"/qualification.json",qualification,{mode:384});
await cli(["resident","enroll",J({generation:0,owner:"fixture-enrollment",parent:P,worker:W,
evidence_file:d+"/qualification.json",evidence_sha256:(await import("node:crypto")).createHash("sha256").update(qualification).digest("hex")})]);
const access=await activate(["client-preflight",d]);
assert.equal(access.filesystemAccess,true);assert.equal(access.sendAuthorized,false);
assert(!(await fs.readdir(d)).some(n=>n.startsWith(".pro-access-")));
let p=await activate(["resident-packet",P,P,mode==="poolturn"?J([W,"resident-pro-b"]):W,d]);
const prompt=d+"/prompt.txt";await fs.writeFile(prompt,mode==="replacementtext"?"Literal replacement text: $& $` $\'":"Fixture answer.",{mode:384});
const tools={
async mcp__node_repl__js(a){
s.repls++;
// Native REPL creates a fresh imported module per call, while native globals
// survive. Ordinary Node import caching hid the module-local WeakMap bug.
if(mode==="freshmodules")a={...a,code:a.code.replace(/parked-activation\.mjs\?sha256=[a-f0-9]+/g,m=>m+"&cell="+s.repls)};
if(mode==="relayidentity"&&a.title==="Serve-existing relay")meta.threadId="foreign-task";
if(a.title==="Guarded serve-existing claim"){
s.serveClaims=(s.serveClaims||0)+1;
if(mode==="servehostempty")return {status:"failed"};
if(mode==="servehostthrow")throw Error("native host transport failed");
if(mode==="servehostnull")return null;
if(mode==="servehostmalformed")return {content:[{type:"text",text:"not JSON"}]};
if(mode==="servehostfailedreceipt")return {status:"failed",...mcp({claimed:true})};
}
if(a.code.includes("activation.residentAdmission(")&&!a.code.includes('"command":')){
s.waits++;events.emit("change");
}
// Incident boundary: after the actual synthetic send, one owner-guard REPL
// reply ("decoder") or the socket finish reply ("transport") is undecodable.
const claim=a.code.includes("Serve consumed");
if(claim)s.claims=(s.claims||0)+1;
if(["closefail","blockedclosefail"].includes(mode)&&a.code.includes("o.socket.close("))return {isError:true,content:[{type:"text",text:"EIO close"}]};
if(!s.poisoned&&((last&&mode==="decoder"&&a.code.includes('console.log("{}")'))||
(last&&mode==="transport"&&a.code.includes("parkedSocket.finish("))||
(["claimlost","claimlostturn"].includes(mode)&&claim)||(mode==="dupelost"&&claim&&s.claims===2)||
(mode==="auditlost"&&a.code.includes("o.socket.close(")))){
s.poisoned=true;
// All but "decoder" execute the code (finish/claim/close happened) but lose
// its reply; "decoder" loses an owner-guard reply.
if(mode!=="decoder")await new AF("nodeRepl","globalThis","console","parkedSocket","parkedBinding",a.code)(
{tmpDir:d,requestMeta:meta},g,{log(){}},g.parkedSocket,g.parkedBinding).catch(()=>{});
if(mode==="claimlostturn")meta["x-codex-turn-metadata"].turn_id="changed";
return {content:[{type:"text",text:"{}"},{type:"text",text:"{}"}],isError:false};
}
const lines=[];
try{
await new AF("nodeRepl","globalThis","console","parkedSocket","parkedBinding",a.code)(
{tmpDir:d,requestMeta:meta},g,{log:v=>lines.push(String(v))},
g.parkedSocket,g.parkedBinding);
}catch(e){
if(mode!=="servehostguarderror")throw e;
s.suppressedNativeError=true;return {status:"failed"};
}
if(mode==="relaylost"&&a.title==="Serve-existing relay")
return {isError:true,content:[{type:"text",text:"relay response lost"}]};
if(mode==="serveclaimlost"&&a.title==="Guarded serve-existing claim")
return {isError:true,content:[{type:"text",text:"claim response lost"}]};
if(mode==="servehostemptyafter"&&a.title==="Guarded serve-existing claim")return {status:"failed"};
if(mode==="replacementlost"&&a.title==="Replace consumed unused serving session")return {status:"failed"};
if(mode==="evidenceerror"&&!s.evidenceError&&a.title==="Preserve native evidence"){
s.evidenceError=true;return {isError:true,content:[{type:"text",text:lines.join("\n")}]};
}
if(mode==="evidencepartial"&&!s.evidenceError&&a.title==="Preserve native evidence"&&a.code.includes("const second=")){
s.evidenceError=true;
const path=JSON.parse(/const second=await fs.open\((.*),"wx",0o600\)/.exec(a.code)[1]);
await fs.unlink(path); // Simulate an envelope surviving without its history.
return {isError:true,content:[]};
}
return {content:[{type:"text",text:lines.join("\n")}]};
},
async exec_command(a){
// Force a nonterminal observation past the budget, without real hour-long waits.
if(["pending","recoverypending"].includes(mode)&&a.cmd.includes("'queue' 'observe'")){
s.tick+=3600001;
return {exit_code:0,output:J({ok:true,request_id:last?.id??"job-A",parent_task_id:P,
worker_conversation_id:W,observation:"pending"})};
}
const pending=run("/bin/sh",["-c",a.cmd]);
if(["beginlost","beginlostwrite","begindrained","beginmalformed"].includes(mode)&&a.cmd.includes("'resident' 'begin'")){
await pending;
if(mode==="beginmalformed")return {exit_code:0,output:"not JSON"};
const id=++serial;sessions.set(id,pending);s.lostSession=id;
if(mode==="begindrained")s.drainOnce=true;
if(mode==="beginlostwrite"){
// The first exclusive evidence write fails; a later independent write can work.
await fs.mkdir(g.parkedResident.directory+"/resident-failure.json",{mode:448});
}
g.parkedResident.admission.expire();await g.parkedResident.admission.failure.catch(()=>{});
return {session_id:id,output:""};
}
if(mode==="helperlost"&&a.cmd.includes("'arm'")){
// The helper yields, then its host continuation is lost for good.
const id=++serial;sessions.set(id,pending);s.lostSession=id;
return {session_id:id,output:""};
}
if(mode==="runnerpending"&&a.cmd.includes("'arm'")){
// The runner's own helper yields and never reports completion.
const id=++serial;sessions.set(id,pending);s.stuckSession=id;
return {session_id:id,output:""};
}
const result=await pending;
if(mode==="poolturn"&&a.cmd.includes("'indeterminate'")){
const reason=/'--reason-file' '([^']+)'/.exec(a.cmd)?.[1];
if(reason)s.failureEvidence=await fs.readFile(reason,"utf8");
}
if(mode==="poolturn"&&a.cmd.includes("'arm-for-send'"))s.armChecksToHold=(s.armChecksToHold||0)+1;
if(a.cmd.includes("'arm'")){
if(mode==="turn")meta["x-codex-turn-metadata"].turn_id="changed";
if(mode==="owner")g.parkedBinding=Object.freeze({...g.parkedBinding});
}
return result;
},
async write_stdin(a){
assert.equal(a.chars,"");assert(sessions.has(a.session_id));
if(a.session_id===s.lostSession){
s.drains=(s.drains||0)+1;
if(!s.drainOnce||s.drains===1)throw Error(s.drains===1?"host lost":"host lost again");
}
if(a.session_id===s.stuckSession){s.drains=(s.drains||0)+1;return {session_id:a.session_id,output:""};}
const r=await sessions.get(a.session_id);sessions.delete(a.session_id);return r;
},
async mcp__codex_app__read_thread(a){
if(mode==="poolturn"){
const entry=s.poolSent?.get(a.threadId);
return mcp({schemaVersion:1,thread:{id:a.threadId,kind:"chatgpt",status:{type:"idle"}},turns:entry?[{id:"turn-"+entry.id,items:[{id:"turn-"+entry.id,type:"userMessage",content:[{type:"text",text:entry.prompt}]},{id:"answer-"+entry.id,type:"agentMessage",text:"[CODEX_PRO_DISPATCH_RESULT assignment_id="+entry.id+"]\nanswer\n[CODEX_PRO_DISPATCH_END assignment_id="+entry.id+"]"}]}]:[]});
}
assert.equal(a.threadId,W);s.reads++;
if(mode==="stoprace"&&last)await stop(); // Explicit stop racing the failure below.
if(mode==="nextqueued"&&last)await publishB(); // B published while A is failing.
if(mode==="externalclose"&&last)await g.parkedSocket.close("unit_external_close");
if(mode==="readthrow"&&last)throw Error("net::ERR_NETWORK_CHANGED");
if(mode==="readinitial"&&!last)return {isError:true,content:[{type:"text",text:"net::ERR_NETWORK_CHANGED"}]};
if(mode==="readonce"&&last&&!s.readFailed){
s.readFailed=true;await stop();
return {isError:true,content:[{type:"text",text:"net::ERR_NETWORK_CHANGED"}]};
}
if(["blocked","stoprace","nextqueued","externalclose","blockedclosefail"].includes(mode)&&last)return {isError:true};
return mcp({schemaVersion:1,thread:{id:W,kind:"chatgpt",status:{type:mode==="busy"&&s.reads===1?"working":"idle"}},
turns:last?[{id:"turn-"+last.id,items:[
{id:"turn-"+last.id,type:"userMessage",content:[{type:"text",text:last.prompt}]},
{id:"answer-"+last.id,type:"agentMessage",text:
"[CODEX_PRO_DISPATCH_RESULT assignment_id="+last.id+"]\nanswer\n"+
"[CODEX_PRO_DISPATCH_END assignment_id="+last.id+"]"}]}]:[]});
},
async mcp__codex_app__send_message_to_thread(a){
if(mode==="poolturn"){
const id=/^\[CODEX_PRO_DISPATCH assignment_id=([^\]]+)\]\n/.exec(a.prompt)?.[1];assert(id);
s.poolSent??=new Map();assert(!s.poolSent.has(a.threadId));s.poolSent.set(a.threadId,{id,prompt:a.prompt});s.sends.push(id);return mcp({});
}
assert.equal(a.threadId,W);
assert.equal(g.parkedResident.admission?.deadlineAt??null,null,"Idle detector must not time out Pro");
const id=/^\[CODEX_PRO_DISPATCH assignment_id=([^\]]+)\]\n/.exec(a.prompt)?.[1];
assert(id);last={id,prompt:a.prompt};s.sends.push(id);
if(mode==="senderror")return {isError:true,content:[{type:"text",text:"net::ERR_NETWORK_CHANGED"}]};
if(mode==="sendthrow")throw Error("Native send outcome unknown");
// Retained incident acknowledgment shape, its empty variant, a foreign thread.
if(mode==="ack")return {content:[{type:"text",text:J({threadId:W})}],isError:false};
if(mode==="ackworker")return mcp({threadId:"another-thread"});
return mcp({});
},
async mcp__codex_app__navigate_to_codex_page(a){
assert.fail("Resident execution must not navigate: "+a.threadId);
}
};
async function open(){
let result;
await new AF("tools","text",p.calls.open)(tools,r=>{result=JSON.parse(r.content[0].text);});
if(!["ready","collect_only"].includes(result.state))throw Error("Resident "+result.state+": "+result.reason);
return g.parkedResident.directory;
}
const Clock=class extends Date{static now(){return Date.now()+s.tick;}};
const serve=(body=p.calls.serve)=>track(new AF("tools","text","Date",body)(
tools,v=>out.push(v),Clock));
const start=(n,id)=>activate(["rendezvous",g.parkedResident.directory,
String(n),id,prompt,"unit-client"]);
const stop=()=>activate(["resident-stop",g.parkedResident.directory]);
const repeat=id=>json(process.execPath,[scripts+"parked-client.mjs",
g.parkedResident.directory,"submit",id,prompt,"unit-client"]);
const reopen=async()=>{
p=await activate(["resident-packet",P,P,mode==="poolturn"?J([W,"resident-pro-b"]):W,d]);
return open();
};
// A client publication for ordinal n without waiting on its readiness
// watcher: exactly what a queued or abandoned client leaves on disk.
async function publish(n,requestId,deadlineAt=Date.now()+30000){
const {createHash}=await import("node:crypto"),dir=g.parkedResident.directory;
const raw=await fs.readFile(prompt);
await fs.mkdir(dir+"/command-"+n,{mode:448}).catch(e=>{if(e.code!=="EEXIST")throw e;});
await fs.writeFile(dir+"/command-"+n+"/prompt.txt",raw,{mode:384});
await fs.writeFile(dir+"/command-"+n+".json",J({sessionId:g.parkedResident.socket.config.sessionId,
ordinal:n,requestId,clientSessionId:"unit-client",nonce:String(n).repeat(32),
deadlineAt,promptSha256:createHash("sha256").update(raw).digest("hex"),
pid:process.pid,ppid:process.ppid}),{mode:384});
}
const publishB=()=>publish(2,"job-B");
const waiting=n=>g.parkedResident.directory+"/waiting-"+n+"."+g.parkedResident.socket.config.sessionId;
return {g,meta,s,out,cli,open,serve,start,stop,repeat,reopen,activate,d,children,waiting,publish,tools,
recovery:()=>activate(["resident-serve-existing-packet",g.parkedResident.directory]),
// Idle means resident-next N is executing and has proven it is waiting.
idle:async n=>{
await until(()=>s.waits>=n);
for(let i=0;;i++){
if((await fs.readdir(g.parkedResident.directory)).some(v=>/^waiting-\d+[.][a-f0-9]{32}$/.test(v)))return;
if(i>=200)throw Error("Resident never proved it was waiting");
await pause(25);
}
}};
}

test("idle admission has no independently living helper process",async t=>{
const f=await fixture(t);await f.open();const serving=f.serve();
await f.idle(1);
try{
assert.equal([...f.children].filter(c=>c.spawnargs.includes("resident-next")||
c.spawnargs.some(a=>a.includes("'resident-next'"))).length,0);
}finally{await f.stop();await serving;}
assert.deepEqual(f.s.sends,[]);
});

test("supervised original serve survives repeated idle boundaries and accepts its first request once",async t=>{
const f=await fixture(t),d=await f.open();
const realTimeout=globalThis.setTimeout;
const timerMock=t.mock.method(globalThis,"setTimeout",(fn,ms,...args)=>
realTimeout(fn,ms===25000?30:ms,...args));
const serving=f.serve();
try{
// Exercise more than the incident's 270 seconds worth of idle observations,
// compressed only at the 25-second idle boundary. Real CLI and socket IO.
await f.idle(13);
assert.equal(f.out[0].kind,"resident_supervision_required");
assert.equal(f.out[0].admissionObserved,false);
assert.equal(f.out[0].lifecycle.detachedSupported,false);
assert.equal(f.g.parkedResident.admission.expired,false);
await missing(d+"/resident-failure.json");await missing(d+"/transport-audit.json");
assert.equal(f.s.reads,0);assert.deepEqual(f.s.sends,[]);
timerMock.mock.restore();
// Wait for the next uncompressed observation before publishing.
await f.idle(f.s.waits+1);
await f.start(1,"job-A");
await waitForFile(f.waiting(2));
}finally{
timerMock.mock.restore();await f.stop();await serving;
}
assert.deepEqual(f.s.sends,["job-A"]);
const audit=JSON.parse(await fs.readFile(d+"/transport-audit.json"));
assert.equal(audit.reason,"resident_stopped");
assert.deepEqual(audit.events.map(e=>e.name),["accepted","finished"]);
await missing(d+"/wake.sock");
});

test("oversized resident prompts preserve the real owner's admission for valid content",async t=>{
const f=await fixture(t),d=await f.open(),serving=f.serve();await f.idle(1);
for(const body of ["x".repeat(20000),"😀".repeat(10000)," \n\t"]){
await fs.writeFile(f.d+"/prompt.txt",body);
await assert.rejects(f.start(1,"job-A"),/native read limit|Prompt is empty/);
for(const name of ["command-1","command-1.json","command-observed-1.json","ready-1.json"])
await missing(d+"/"+name);
assert.deepEqual((await f.cli(["queue","status"])).requests,[]);
}
await fs.writeFile(f.d+"/prompt.txt","valid request");await f.start(1,"job-A");await f.idle(2);
await f.stop();await serving;assert.deepEqual(f.s.sends,["job-A"]);
});

test("resident status distinguishes open, waiting, busy and closed without granting authority",async t=>{
const f=await fixture(t),d=await f.open();
const status=()=>f.activate(["resident-status",d]);
const before=(await f.cli(["resident","inspect"])).owner;
assert.equal((await status()).state,"not_waiting");
const serving=f.serve();await f.idle(1);
const waiting=await status();
assert.equal(waiting.state,"admission_observed");assert.equal(waiting.ordinal,1);
assert.equal(waiting.maxConcurrentRequests,1);assert.equal(waiting.admissionObserved,true);
assert.equal(waiting.sendAuthorized,false);assert.equal(waiting.replacementAuthorized,false);
assert(!JSON.stringify(waiting).includes(f.g.parkedSocket.config.token));
assert.deepEqual((await f.cli(["resident","inspect"])).owner,before);
assert.deepEqual((await f.cli(["queue","status"])).requests,[]);
const c={...f.g.parkedResident.credentials,invocation:"test-status-reservation",request:"job-A"};
await f.cli(["resident","begin",J(c)]);
assert.equal((await status()).state,"busy");
assert.equal((await f.cli(["resident","inspect"])).owner.inflight.request,"job-A");
await f.cli(["resident","end",J(c)]);
await f.stop();await serving;
assert.equal((await status()).state,"closed_audited");
assert.deepEqual(f.s.sends,[]);
});

test("retired generation with a socket and fresh marker cannot publish or masquerade as capacity",async t=>{
const f=await fixture(t),d=await f.open(),c=f.g.parkedResident.credentials;
await fs.mkdir(f.waiting(1),{mode:448});
const replacement=(await f.cli(["resident","start",J({...c,owner:"replacement-fixture"})])).owner;
const other=f.d+"/new-session";await fs.mkdir(other,{mode:448});
const descriptor={...f.g.parkedSocket.config,sessionId:"a".repeat(32)};
await fs.writeFile(other+"/session.json",J(descriptor),{mode:384});
await f.cli(["resident","bind-session",J({...replacement,session:{directory:other,
session_id:descriptor.sessionId,descriptor_sha256:(await import("node:crypto")).createHash("sha256").update(J(descriptor)).digest("hex")}})]);
// Copying the current owner's identity into the old folder must not rebind it.
await fs.writeFile(d+"/session.json",J({...descriptor,residentOwner:replacement}));
const before=await f.cli(["resident","inspect"]),files=await fs.readdir(d);
const result=await f.activate(["resident-status",d]);
assert.equal(result.reason,"retired_owner");assert.equal(result.admissionObserved,false);
await assert.rejects(f.start(1,"job-A"),/retired_owner/);
assert.deepEqual(await fs.readdir(d),files);
assert.deepEqual(await f.cli(["resident","inspect"]),before);
await fs.lstat(d+"/wake.sock");await fs.lstat(f.waiting(1));
assert.deepEqual((await f.cli(["queue","status"])).requests,[]);
assert.deepEqual(f.s.sends,[]);
});

for(const mode of ["legacy","helper","parent","identity","descriptor","symlink","malformed","future","stale","conflict","stopped"])
test("listener inspection preserves unavailable evidence: "+mode,async t=>{
const f=await fixture(t),d=await f.open(),path=d+"/session.json";
const c=JSON.parse(await fs.readFile(path,"utf8"));
if(mode==="legacy"){
const path=f.d+"/authority/state/resident-owner.json",v=JSON.parse(await fs.readFile(path));
delete v.session;v.version=1;await fs.writeFile(path,J(v));
}
if(mode==="helper")c.helper+=".other";
if(mode==="parent")c.parent="different-parent";
if(mode==="identity")c.sessionId="b".repeat(32);
if(mode==="descriptor")c.token="b".repeat(48);
if(["helper","parent","identity","descriptor"].includes(mode))await fs.writeFile(path,J(c));
if(mode==="malformed")await fs.writeFile(path,"{}");
if(["future","stale","conflict"].includes(mode)){
await fs.mkdir(f.waiting(1),{mode:448});
const time=new Date(Date.now()+(mode==="future"?60000:mode==="stale"?-60000:0));
await fs.utimes(f.waiting(1),time,time);
if(mode==="conflict")await fs.writeFile(d+"/command-1.json","{}",{mode:384});
}
if(mode==="stopped")await fs.writeFile(d+"/resident-stop.json",J({sessionId:c.sessionId}),{mode:384});
let input=d;
if(mode==="symlink"){input=f.d+"/alias";await fs.symlink(d,input);}
const before=await fs.readFile(path),files=await fs.readdir(d),owner=await f.cli(["resident","inspect"]);
const result=await f.activate(["resident-status",input]);
assert.equal(result.admissionObserved,false);assert.equal(result.replacementAuthorized,false);
assert.equal(result.reason,({legacy:"unbound_owner",helper:"installed_helper_mismatch",
parent:"retired_owner",identity:"retired_owner",descriptor:"descriptor_changed",future:"waiter_not_fresh",stale:"waiter_not_fresh",
stopped:"stop_or_failure_evidence"})[mode]??"evidence_unverifiable");
if(["legacy","helper","parent","identity","descriptor"].includes(mode))
await assert.rejects(f.start(1,"job-A"),/Resident unavailable/);
assert.deepEqual(await fs.readFile(path),before);assert.deepEqual(await fs.readdir(d),files);
assert.deepEqual(await f.cli(["resident","inspect"]),owner);
assert.deepEqual((await f.cli(["queue","status"])).requests,[]);assert.deepEqual(f.s.sends,[]);
});

test("bounded idle observations renew only from their owner and abandoned idle closes",async t=>{
const f=await fixture(t),old=await f.open(),o=f.g.parkedResident;
o.used=true;o.serveInvocation="fixture-owner";
const {residentAdmission}=await import(author);
const realTimeout=globalThis.setTimeout;
const timerMock=t.mock.method(globalThis,"setTimeout",(fn,ms,...args)=>
realTimeout(fn,ms===25000?100:ms===60000?1000:ms,...args));
const next=()=>residentAdmission(f.g,f.meta,"fixture-owner",1);
for(let i=0;i<3;i++){
const wait=next();await waitForFile(f.waiting(1));
assert.deepEqual(await wait,{pending:true,sessionId:o.socket.config.sessionId});
await missing(f.waiting(1));
await missing(old+"/transport-audit.json");
}
// The outer caller never re-enters. Native code must not renew itself.
await new Promise(r=>realTimeout(r,1100));
await o.admission.failure;
await missing(f.waiting(1));await missing(old+"/wake.sock");
assert.equal(JSON.parse(await fs.readFile(old+"/transport-audit.json")).reason,"resident_failed");
const before=await fs.readFile(old+"/resident-failure.json");
assert.equal(JSON.parse(before).heldDelivery,null);
await assert.rejects(next(),/ended or occupied/);
assert.deepEqual(await fs.readFile(old+"/resident-failure.json"),before);
timerMock.mock.restore();
assert.deepEqual(f.s.sends,[]);
assert.deepEqual((await f.cli(["queue","status"])).requests,[]);
// Existing cooperative recovery works; no new takeover protocol is needed.
assert.notEqual(await f.reopen("failed-resident-packet"),old);
const serving=f.serve();await f.idle(1);await f.stop();await serving;
});

for(const ending of ["expiry","stop"])
test("retained sibling waiter preserves identity and withdraws on "+ending,async t=>{
const f=await fixture(t),d=await f.open(),o=f.g.parkedResident;
o.used=true;o.serveInvocation="fixture-owner";
const next=(ordinal=1,meta=f.meta)=>o.activation.residentAdmission(f.g,meta,"fixture-owner",ordinal,undefined,true);
assert.equal((await next()).pending,true);
const work=o.admission.work,marker=await fs.stat(f.waiting(1)),deadline=o.admission.deadlineAt;
await assert.rejects(next(2),/ended or occupied/);
await assert.rejects(next(1,{...f.meta,threadId:"foreign"}),/Wrong resident admission owner/);
await assert.rejects(next(1,{...f.meta,"x-codex-turn-metadata":{turn_id:"foreign"}}),/Wrong resident admission owner/);
assert.equal(o.admission.deadlineAt,deadline);
assert.equal((await next()).pending,true);
assert.equal(o.admission.work,work);
assert.equal((await fs.stat(f.waiting(1))).ino,marker.ino);
if(ending==="expiry"){
o.admission.expire();await o.admission.failure;
await missing(d+"/wake.sock");
await assert.rejects(next(),/ended or occupied/);
}else{
await f.stop();assert.equal((await next()).stopped,true);
}
await missing(f.waiting(1));assert.deepEqual(f.s.sends,[]);
});

test("a returned stop keeps the detector until owner cleanup finishes",async t=>{
const f=await fixture(t),d=await f.open(),o=f.g.parkedResident;
o.used=true;o.serveInvocation="fixture-owner";
const realTimeout=globalThis.setTimeout;
const timerMock=t.mock.method(globalThis,"setTimeout",(fn,ms,...args)=>
realTimeout(fn,ms===60000?1000:ms,...args));
const waiting=o.activation.residentAdmission(f.g,f.meta,"fixture-owner",1);
await waitForFile(f.waiting(1));await f.stop();
assert.equal((await waiting).stopped,true);
assert.notEqual(o.admission.deadlineAt,null);
await missing(f.waiting(1));
// No outer finally follows the returned stop. The retained detector must
// close it truthfully as failed, not pretend that clean shutdown completed.
await new Promise(r=>realTimeout(r,1100));await o.admission.failure;
timerMock.mock.restore();
await missing(d+"/wake.sock");
assert.equal(JSON.parse(await fs.readFile(d+"/transport-audit.json")).reason,"resident_failed");
assert.equal(JSON.parse(await fs.readFile(d+"/resident-failure.json")).stopRequested,true);
assert.notEqual(await f.reopen("failed-resident-packet"),d);
assert.deepEqual(f.s.sends,[]);
assert.deepEqual((await f.cli(["queue","status"])).requests,[]);
});

test("a client claim wins over the idle observation boundary",async t=>{
const f=await fixture(t),d=await f.open(),o=f.g.parkedResident;
o.used=true;o.serveInvocation="fixture-owner";
const realTimeout=globalThis.setTimeout;
const timerMock=t.mock.method(globalThis,"setTimeout",(fn,ms,...args)=>
realTimeout(fn,ms===25000?100:ms,...args));
let settled=false;
const waiting=o.activation.residentAdmission(f.g,f.meta,"fixture-owner",1);
waiting.then(()=>{settled=true;},()=>{settled=true;});
await waitForFile(f.waiting(1));
await fs.rename(f.waiting(1),d+"/command-1");
await new Promise(r=>realTimeout(r,200));
assert.equal(settled,false,"An idle boundary must not strand a winning claimant");
await f.publish(1,"job-A");
const next=await waiting;timerMock.mock.restore();
assert.equal(next.command.requestId,"job-A");assert.equal(next.pending,undefined);
await fs.lstat(d+"/command-1");await fs.lstat(d+"/command-1.json");
assert.deepEqual(f.s.sends,[]);
});

test("owner loss during a native wait retires the marker before failure proof",async t=>{
const f=await fixture(t),d=await f.open(),o=f.g.parkedResident;
o.used=true;o.serveInvocation="fixture-owner";
const {residentAdmission}=await import(author);
const waiting=residentAdmission(f.g,f.meta,"fixture-owner",1);
const rejected=assert.rejects(waiting,/stopped renewing admission/);
await waitForFile(f.waiting(1));
o.admission.expire();await rejected;await o.admission.failure;
await missing(f.waiting(1));await missing(d+"/wake.sock");
await assert.rejects(f.start(1,"never-sent"),/Existing rendezvous artifact|ENOENT/);
assert.deepEqual((await f.cli(["queue","status"])).requests,[]);
assert.deepEqual(f.s.sends,[]);
});

test("a resumed stale owner cannot renew before a delayed timer runs",async t=>{
const f=await fixture(t),d=await f.open(),o=f.g.parkedResident;
o.used=true;o.serveInvocation="fixture-owner";
const {residentAdmission}=await import(author);
const realTimeout=globalThis.setTimeout;
const timerMock=t.mock.method(globalThis,"setTimeout",(fn,ms,...args)=>
realTimeout(fn,ms===25000?100:ms,...args));
const waiting=residentAdmission(f.g,f.meta,"fixture-owner",1);
await waitForFile(f.waiting(1));await waiting;
o.admission.deadlineAt=Date.now()-1;
await assert.rejects(residentAdmission(f.g,f.meta,"fixture-owner",1),/ended or occupied/);
await o.admission.failure;timerMock.mock.restore();
await missing(d+"/wake.sock");assert.deepEqual(f.s.sends,[]);
});

test("owner loss while receive waits closes it and preserves observed-command recovery blocker",async t=>{
const f=await fixture(t),d=await f.open(),o=f.g.parkedResident;
o.used=true;o.serveInvocation="fixture-owner";
const {residentAdmission}=o.activation;
const waiting=residentAdmission(f.g,f.meta,"fixture-owner",1);
await waitForFile(f.waiting(1));
await fs.rename(f.waiting(1),d+"/command-1");await f.publish(1,"job-A");
const next=await waiting;
await f.cli(["resident","begin",J({...o.credentials,invocation:"fixture-owner",request:"job-A"})]);
const receiving=residentAdmission(f.g,f.meta,"fixture-owner",1,next);
const rejected=assert.rejects(receiving,/stopped renewing admission/);
await waitForFile(d+"/ready-1.json");
o.admission.expire();await rejected;await o.admission.failure;
await missing(d+"/wake.sock");
await assert.rejects(f.reopen(),/busy/);
await fs.lstat(d+"/command-observed-1.json");
assert.deepEqual(f.s.sends,[]);
});

test("cleanup closes an active receive before joining it",async t=>{
const f=await fixture(t),d=await f.open(),o=f.g.parkedResident;
o.used=true;o.serveInvocation="fixture-owner";
const waiting=o.activation.residentAdmission(f.g,f.meta,"fixture-owner",1);
await waitForFile(f.waiting(1));
await fs.rename(f.waiting(1),d+"/command-1");await f.publish(1,"job-A");
const next=await waiting;
await f.cli(["resident","begin",J({...o.credentials,invocation:"fixture-owner",request:"job-A"})]);
const receiving=o.activation.residentAdmission(f.g,f.meta,"fixture-owner",1,next);
const rejected=assert.rejects(receiving,/Resident admission stopped/);
await waitForFile(d+"/ready-1.json");
await o.activation.stopResidentAdmission(o);await rejected;
await missing(d+"/wake.sock");
await fs.lstat(d+"/ready-1.json");await fs.lstat(d+"/command-observed-1.json");
assert.equal(o.admission.expired,true);assert.equal(o.admission.deadlineAt,null);
assert.deepEqual(f.s.sends,[]);
});

test("owner loss preserves a concurrently published stop without claiming a clean stop",async t=>{
const f=await fixture(t),d=await f.open(),o=f.g.parkedResident;
o.used=true;o.serveInvocation="fixture-owner";
const realTimeout=globalThis.setTimeout;
const timerMock=t.mock.method(globalThis,"setTimeout",(fn,ms,...args)=>
realTimeout(fn,ms===25000?100:ms,...args));
await o.activation.residentAdmission(f.g,f.meta,"fixture-owner",1);
timerMock.mock.restore();
await f.stop();o.admission.expire();await o.admission.failure;
const failure=JSON.parse(await fs.readFile(d+"/resident-failure.json"));
assert.equal(failure.stopRequested,true);assert.equal(failure.reason,"resident_failed");
assert.notEqual(await f.reopen("failed-resident-packet"),d);
assert.deepEqual(f.s.sends,[]);
});

test("owner expiry withdraws readiness even while its filesystem check is stalled",async t=>{
const f=await fixture(t),d=await f.open(),o=f.g.parkedResident;
o.used=true;o.serveInvocation="fixture-owner";
let release,entered=false;
const barrier=new Promise(r=>{release=r;}),original=nativeFs.readdir;
const hook=t.mock.method(nativeFs,"readdir",async(...args)=>{
if(args[0]===d&&!entered){entered=true;await barrier;}
return await original(...args);
});
syncBuiltinESMExports();
try{
const waiting=o.activation.residentAdmission(f.g,f.meta,"fixture-owner",1);
const rejected=assert.rejects(waiting,/stopped renewing admission/);
while(!entered)await new Promise(r=>setImmediate(r));
o.admission.expire();
await waitForFile(d+"/transport-audit.json");
// The blocked filesystem reader is still live, but cannot keep advertising.
for(let i=0;;i++){
try{await fs.lstat(f.waiting(1));}catch(e){if(e.code==="ENOENT")break;throw e;}
if(i>2000)throw Error("Marker was not withdrawn");
await new Promise(r=>setImmediate(r));
}
await missing(d+"/wake.sock");await missing(d+"/resident-failure.json");
release();await rejected;await o.admission.failure;
await fs.lstat(d+"/resident-failure.json");assert.deepEqual(f.s.sends,[]);
}finally{release();hook.mock.restore();syncBuiltinESMExports();}
});


test("interrupted unserved owner is replaced without a cancellation recipe",async t=>{
const f=await fixture(t),old=await f.open();
f.meta["x-codex-turn-metadata"].turn_id="next-turn";
assert.notEqual(await f.reopen(),old);
await missing(old+"/wake.sock");
const serving=f.serve();await f.idle(1);await f.stop();await serving;
assert.deepEqual(f.s.sends,[]);
});

test("old artifacts survive replacement and do not grant send permission",async t=>{
const f=await fixture(t),old=await f.open();
await fs.writeFile(old+"/command-1.json","uninterpreted old evidence",{mode:384});
assert.notEqual(await f.reopen(),old);
assert.equal(await fs.readFile(old+"/command-1.json","utf8"),"uninterpreted old evidence");
assert.deepEqual((await f.cli(["queue","status"])).requests,[]);
assert.deepEqual(f.s.sends,[]);
});

test("unfenced retained legacy socket is not enrolled",async t=>{
const f=await fixture(t);await f.open();
delete f.g.parkedResident.credentials;
await assert.rejects(f.reopen(),/Unfenced legacy/);
assert.deepEqual(f.s.sends,[]);
});

for(const ids of [[],["job-A","job-B"]])
test("resident unknown-request cycle: "+ids.join(","),async t=>{
const f=await fixture(t),d=await f.open();
assert.equal(f.g.parkedResident.used,false);
const serving=f.serve();await f.idle(1);
// A duplicate serve is refused and never closes or cleans up the running owner.
await assert.rejects(f.serve(),/Serve consumed/);
await missing(d+"/transport-audit.json");await missing(d+"/resident-failure.json");
assert(!f.out.some(v=>v.kind==="resident_closed"));
assert.equal(f.s.reads,0);
for(let i=0;i<ids.length;i++){
if(i){
const quiet=[f.s.reads,f.s.repls];await pause(200);
assert.deepEqual([f.s.reads,f.s.repls],quiet);
assert(!(await f.cli(["queue","status"])).requests.some(v=>v.request_id===ids[i]));
}
await f.start(i+1,ids[i]);await f.idle(i+2);
assert(!f.out.some(v=>v.kind==="runner_receipt"||v.kind==="active_job_observation"));
assert(!JSON.stringify(f.out).includes("CODEX_PRO_DISPATCH_RESULT"));
assert(!JSON.stringify(f.out).includes('"payload"'));
const collected=await f.cli(["queue","collect",ids[i]]);
assert(JSON.stringify(collected).includes("answer"));
const a=(await f.cli(["status",ids[i]])).assignment;
assert.equal(a.submission_count,1);assert.equal(a.no_resend,true);
}
await f.stop();await serving;
await assert.rejects(f.serve());
assert.deepEqual(f.s.sends,ids);
if(!ids.length)assert.equal(f.s.reads,0);
assert.equal(f.g.parkedDelivery,null);
assert.equal(JSON.parse(await fs.readFile(d+"/transport-audit.json","utf8")).reason,"resident_stopped");
await missing(d+"/ready-"+(ids.length+1)+".json");await missing(d+"/wake.sock");
});

test("clean unused resident can restart without replaying old serve",async t=>{
const f=await fixture(t),old=await f.open(),first=f.serve();
await f.idle(1);await f.stop();await first;
const fresh=await f.reopen();assert.notEqual(fresh,old);
const second=f.serve();await f.idle(2);
await f.stop();await second;
assert.deepEqual(f.s.sends,[]);
await missing(old+"/wake.sock");
assert.equal((await f.cli(["resident","inspect"])).owner.generation,3);
await missing(old+"/replacement-open.once.json");
});

test("stopped resident preserves and skips multiple expired unobserved commands",async t=>{
const f=await fixture(t),old=await f.open(),first=f.serve(),{createHash}=await import("node:crypto");
await f.idle(1);await f.stop();await first;
for(let n=1;n<=3;n++){
const prompt="Unobserved "+n,base=old+"/command-"+n;
await fs.mkdir(base,{mode:448});
await fs.writeFile(base+"/prompt.txt",prompt,{mode:384});
await fs.writeFile(base+".json",J({
sessionId:f.g.parkedResident.socket.config.sessionId,ordinal:n,
requestId:"unobserved-"+n,clientSessionId:"unit-client-"+n,
nonce:String(n).padStart(32,"0"),deadlineAt:1,
promptSha256:createHash("sha256").update(prompt).digest("hex"),pid:1,ppid:1
}),{mode:384});
}
assert.notEqual(await f.reopen(),old);
assert.deepEqual((await f.cli(["queue","status"])).requests,[]);
assert.deepEqual(f.s.sends,[]);
});

// Incident shape: open and serve were reported, wake.sock exists, but no
// resident-next is waiting. The client must refuse before publishing anything.
test("open socket without an active waiter refuses to publish a command",async t=>{
const f=await fixture(t),d=await f.open();
await fs.lstat(d+"/wake.sock");
await assert.rejects(f.start(1,"job-A"),/not waiting for ordinal 1; do not publish/);
// A foreign session's marker or a forged file is not readiness either.
await fs.mkdir(d+"/waiting-1."+"0".repeat(32),{mode:448});
await assert.rejects(f.start(1,"job-A"),/not waiting for ordinal 1; do not publish/);
await fs.rmdir(d+"/waiting-1."+"0".repeat(32));
await fs.writeFile(f.waiting(1),"{}",{mode:384});
await assert.rejects(f.start(1,"job-A"),/Stale resident readiness for ordinal 1; do not publish/);
await fs.rm(f.waiting(1));
await missing(d+"/command-1.json");await missing(d+"/command-1");
await missing(d+"/command-observed-1.json");await missing(d+"/ready-1.json");
assert.deepEqual((await f.cli(["queue","status"])).requests,[]);
assert.deepEqual(f.s.sends,[]);
// Nothing was burned: the same request ID is served once a waiter exists.
const serving=f.serve();await f.idle(1);
await f.start(1,"job-A");await f.idle(2);
assert.equal((await f.cli(["queue","collect","job-A"])).state,"published");
await f.stop();await serving;
assert.deepEqual(f.s.sends,["job-A"]);
});

// Legacy crash evidence remains a blocker. No cleanup silently retires an
// unknown marker merely because the current implementation no longer uses a child.
test("stale readiness from a legacy waiter remains evidence",async t=>{
const f=await fixture(t),d=await f.open();
const marker=f.waiting(1);
await fs.mkdir(marker,{mode:448});
assert((await fs.lstat(marker)).isDirectory());
// Heartbeat stopped with the process; once stale the client fails closed.
const stale=new Date(Date.now()-6000);await fs.utimes(marker,stale,stale);
await assert.rejects(f.start(1,"job-A"),/Stale resident readiness for ordinal 1; do not publish/);
await missing(d+"/command-1.json");await missing(d+"/command-1");
assert.deepEqual((await f.cli(["queue","status"])).requests,[]);
// The crash evidence stays; a later serve cannot silently take the ordinal.
await fs.lstat(marker);
await assert.rejects(f.serve(),/EEXIST/);
assert.equal(f.out.find(v=>v.kind==="resident_closed")?.outcome,"resident_failed");
assert.deepEqual(f.s.sends,[]);
});

// The decisive boundary: the client has read fresh readiness but not yet
// claimed it when the production waiter returns. Retirement and claim are both
// atomic on one marker, so the client finds nothing to claim and creates no
// ticket, command, queue request or send; the same request ID then completes
// once a real waiter exists again.
test("waiter exit between readiness and publication leaves nothing behind",async t=>{
const f=await fixture(t),old=await f.open(),first=f.serve();
await f.idle(1);
const hook=f.d+"/claim-pause.cjs",paused=f.d+"/paused",release=f.d+"/release";
// Child-only barrier before the helper takes its admission lock and claims.
await fs.writeFile(hook,`
const cp=require("node:child_process"),fs=require("node:fs");
const execFile=cp.execFile;
cp.execFile=function(file,args,...rest){
if(args[1]==="resident"&&args[2]==="admit"){
fs.writeFileSync(${J(paused)},"");
const limit=Date.now()+20000;
while(!fs.existsSync(${J(release)})){
if(Date.now()>limit)throw Error("Fixture barrier timed out");
Atomics.wait(new Int32Array(new SharedArrayBuffer(4)),0,0,10);
}
}
return execFile(file,args,...rest);
};
require("node:module").syncBuiltinESMExports();
`);
const client=assert.rejects(f.activate(["rendezvous",old,"1","job-A",f.d+"/prompt.txt","unit-client"],
["--require",hook]),/Existing rendezvous artifact; do not reuse/);
for(let i=0;;i++){try{await fs.lstat(paused);break;}catch{if(i>=400)throw Error("Client never paused");await pause(25);}}
// Retirement wins. The locked helper sees the closure audit before claiming.
await f.stop();await first;
await missing(f.waiting(1));
await fs.writeFile(release,"");
await client;
await missing(old+"/command-1");await missing(old+"/command-1.json");
await missing(old+"/command-observed-1.json");await missing(old+"/ready-1.json");
assert.deepEqual((await f.cli(["queue","status"])).requests,[]);
assert.deepEqual(f.s.sends,[]);
// Nothing was burned: the same request ID completes through a real waiter.
assert.notEqual(await f.reopen(),old);
const second=f.serve();await f.idle(2);
await f.start(1,"job-A");await f.idle(3);
assert.equal((await f.cli(["queue","collect","job-A"])).state,"published");
await f.stop();await second;
assert.deepEqual(f.s.sends,["job-A"]);
});

// The other side of the same race: a claim that lands before the stop keeps
// the waiter responsible for that publication instead of returning stopped.
// A claimant that never publishes fails the residence with the ticket kept.
test("claimed waiter stays past a later stop until its command is published",async t=>{
const f=await fixture(t),d=await f.open(),serving=f.serve();
await f.idle(1);
await fs.rename(f.waiting(1),d+"/command-1"); // The client's atomic claim.
await f.stop();
await pause(1500);
assert.equal(f.out.some(v=>v.kind==="resident_closed"),false);
await assert.rejects(serving,/Claimed readiness never published/);
assert.equal(f.out.find(v=>v.kind==="resident_closed")?.outcome,"resident_failed");
await fs.lstat(d+"/command-1");await missing(d+"/command-1.json");
assert.deepEqual(f.s.sends,[]);
});

// A claimant that dies right after its atomic claim, with no stop anywhere:
// the waiter notices its marker is gone, waits the bounded window for the
// command, then fails the residence with the empty ticket kept. Nothing is
// sent or replayed, and the ordinal is never silently retaken.
test("claimant crash after the claim fails the resident within the bound",async t=>{
const f=await fixture(t),d=await f.open(),serving=f.serve();
await f.idle(1);
const at=Date.now();
await fs.rename(f.waiting(1),d+"/command-1"); // Claim, then the client is gone.
await assert.rejects(serving,/Claimed readiness never published/);
assert(Date.now()-at<15000);
assert.equal(f.out.find(v=>v.kind==="resident_closed")?.outcome,"resident_failed");
assert.deepEqual(await fs.readdir(d+"/command-1"),[]);
await missing(d+"/command-1.json");await missing(d+"/command-observed-1.json");
await missing(d+"/ready-1.json");await missing(d+"/resident-stop.json");
assert.deepEqual((await f.cli(["queue","status"])).requests,[]);
// No replay: the closed resident refuses clients, and the claimed ticket
// blocks replacement while staying exactly as it was.
await assert.rejects(f.start(1,"job-A"),/Existing rendezvous artifact/);
assert.notEqual(await f.reopen(),d);
assert.deepEqual(await fs.readdir(d+"/command-1"),[]);await missing(d+"/command-1.json");
assert.deepEqual(f.s.sends,[]);
});

// Incident class: the residence failed having captured no request and holding
// no delivery, while a client publication for ordinal 1 sits unobserved,
// expired, and absent from canonical state. Only the explicit failed-resident
// action replaces it, once, in the same task, preserving all old evidence.
async function failedResidence(t){
const f=await fixture(t),old=await f.open(),serving=f.serve();
await f.idle(1);
await fs.rename(f.waiting(1),old+"/command-1"); // Claimed, then published too late.
await f.publish(1,"job-A",1);
await assert.rejects(serving,/Claimed readiness never published/);
const failure=JSON.parse(await fs.readFile(old+"/resident-failure.json","utf8"));
assert.equal(failure.requestId,null);assert.equal(failure.heldDelivery,null);
assert.equal(failure.stopRequested,false);
assert.equal(JSON.parse(await fs.readFile(old+"/transport-audit.json","utf8")).reason,"resident_failed");
await missing(old+"/wake.sock");
return {f,old,failure};
}


test("zero-send failure uses ordinary startup and preserves evidence",async t=>{
const {f,old,failure}=await failedResidence(t);
const before=await fs.readFile(old+"/command-1.json");
const fresh=await f.reopen();assert.notEqual(fresh,old);
assert.deepEqual(await fs.readFile(old+"/command-1.json"),before);
assert.deepEqual(JSON.parse(await fs.readFile(old+"/resident-failure.json")),failure);
const second=f.serve();await f.idle(2);
await f.start(1,"job-B");await f.idle(3);await f.stop();await second;
assert.deepEqual(f.s.sends,["job-B"]);
assert.deepEqual((await f.cli(["queue","status"])).requests.map(r=>r.request_id),["job-B"]);
});

test("waiting for client permission creates no request or pickup deadline",async t=>{
const f=await fixture(t),d=await f.open(),serving=f.serve();
await f.idle(1);
const before=[f.s.reads,f.s.repls];
await pause(200); // Client has not executed rendezvous, as during an approval wait.
assert.deepEqual([f.s.reads,f.s.repls],before);
assert.deepEqual(f.s.sends,[]);
assert.deepEqual((await f.cli(["queue","status"])).requests,[]);
await missing(d+"/command-1.json");
await missing(d+"/command-observed-1.json");
await missing(d+"/ready-1.json");
// An approval wait longer than the readiness freshness window is fine while
// the waiter's heartbeat keeps its readiness current.
await pause(5200);
await f.start(1,"job-A");await f.idle(2);
assert.equal((await f.cli(["queue","collect","job-A"])).state,"published");
// Denial/cancellation uses the normal stop, not an alternate submit route.
await f.stop();await serving;
assert.deepEqual(f.s.sends,["job-A"]);
await missing(d+"/wake.sock");
});

test("completed acknowledged jobs allow restart and independent new work",async t=>{
const f=await fixture(t),old=await f.open(),first=f.serve();
await f.idle(1);await f.start(1,"job-A");await f.idle(2);
await f.cli(["queue","acknowledge","job-A"]);
await f.stop();await first;
assert.notEqual(await f.reopen(),old);
const second=f.serve();await f.idle(3);
await f.start(1,"job-B");await f.idle(4);
await f.stop();await second;
assert.deepEqual(f.s.sends,["job-A","job-B"]);
});

test("unacknowledged answer survives replacement and stays collectable",async t=>{
const f=await fixture(t);await f.open();const serving=f.serve();
await f.idle(1);await f.start(1,"job-A");await f.idle(2);
await f.stop();await serving;
await f.reopen();
assert.equal((await f.cli(["queue","collect","job-A"])).state,"published");
assert.deepEqual(f.s.sends,["job-A"]);
});

test("uninterpreted old command evidence survives safe replacement",async t=>{
const f=await fixture(t),d=await f.open(),serving=f.serve();
await f.idle(1);await f.stop();await serving;
await fs.writeFile(d+"/command-1.json","{}",{mode:384});
assert.notEqual(await f.reopen(),d);
assert.equal(await fs.readFile(d+"/command-1.json","utf8"),"{}");
await missing(d+"/replacement-open.once.json");
});

test("client preflight rejects shared or symlinked roots without leaving artifacts",async t=>{
const f=await fixture(t),root=f.d+"/client";
await fs.mkdir(root,{mode:0o755});await fs.chmod(root,0o755);
await assert.rejects(f.activate(["client-preflight",root]),/owner-only/);
assert.deepEqual(await fs.readdir(root),[]);
await fs.chmod(root,0o700);await fs.symlink(root,f.d+"/alias");
await assert.rejects(f.activate(["client-preflight",f.d+"/alias"]),/owner-only/);
assert.deepEqual(await fs.readdir(root),[]);
});


test("in-flight canonical reservation blocks replacement, regardless of empty globals",async t=>{
const f=await fixture(t),d=await f.open(),c=f.g.parkedResident.credentials;
await f.cli(["resident","begin",J({...c,invocation:"paused-native-execution",request:"job-A"})]);
f.g.parkedDelivery=null;
await assert.rejects(f.reopen(),/busy/);
assert.equal((await f.cli(["resident","inspect"])).owner.generation,c.generation);
await fs.lstat(d+"/wake.sock");assert.deepEqual(f.s.sends,[]);
});

for(const mode of ["pending","blocked","readthrow","senderror","sendthrow","owner","turn","decoder","transport","ackworker","stoprace","externalclose","runnerpending","helperlost","blockedclosefail","evidenceerror"])
test("failure releases only joined work and cannot admit another request: "+mode,async t=>{
const f=await fixture(t,mode),d=await f.open(),serving=f.serve();
await f.idle(1);
const [served]=await Promise.allSettled([serving,f.start(1,"job-A")]);
assert.equal(served.status,"rejected");
assert.equal(f.s.waits,1);
const state=(await f.cli(["resident","inspect"])).owner;
const joined=["pending","blocked","readthrow","stoprace","externalclose","owner","turn","decoder","transport","ackworker"].includes(mode);
if(joined){
assert.equal(state.inflight,null);
assert.notEqual(await f.reopen(),d);
assert.equal(f.g.parkedResident.collectOnly,mode!=="transport");
assert.equal(f.g.parkedResident.recover,mode==="transport"?null:"job-A");
}else{
assert.equal(state.inflight.request,"job-A");
await assert.rejects(f.reopen(),/busy/);
assert.equal((await f.cli(["resident","inspect"])).owner.generation,state.generation);
}
const a=(await f.cli(["status","job-A"])).assignment;
assert.equal(a.no_resend,true);
assert(["armed","indeterminate","complete"].includes(a.status));
assert.deepEqual(f.s.sends,["owner","turn","runnerpending","helperlost"].includes(mode)?[]:["job-A"]);
await missing(d+"/ready-2.json");
assert(!JSON.stringify(f.out).includes('"payload"'));
assert.equal(f.out.find(v=>v.kind==="resident_closed").reservation_retained,!joined);
});

test("read failure before arm releases only the runner reservation",async t=>{
const f=await fixture(t,"readinitial");await f.open();const serving=f.serve();await f.idle(1);
const [served]=await Promise.allSettled([serving,f.start(1,"job-A")]);
assert.equal(served.status,"rejected");assert.deepEqual(f.s.sends,[]);
assert.equal((await f.cli(["resident","inspect"])).owner.inflight,null);
assert.equal((await f.cli(["status","job-A"])).assignment.status,"prepared");
await f.reopen();assert.equal(f.g.parkedResident.recover,"job-A");
assert.equal(f.g.parkedResident.collectOnly,false);
});

test("network read failure during stop recovers the same answer without resending",async t=>{
const f=await fixture(t,"readonce"),old=await f.open(),first=f.serve();await f.idle(1);
const [served]=await Promise.allSettled([first,f.start(1,"job-A")]);
assert.equal(served.status,"rejected");
const receipt=(await f.cli(["status","job-A"])).assignment;
assert.equal(receipt.status,"indeterminate");assert.equal(receipt.no_resend,true);
assert.equal((await f.cli(["resident","inspect"])).owner.inflight,null);
const preserved=await fs.readFile(old+"/resident-failure-final.json");
const failure=JSON.parse(preserved);assert.equal(failure.nativeUncertain,false);
assert.equal(failure.helperUncertain,false);assert.equal(failure.pendingHelperSession,null);
await missing(old+"/ready-2.json");await missing(old+"/wake.sock");
assert.notEqual(await f.reopen(),old);assert.equal(f.g.parkedResident.collectOnly,true);
assert.equal(f.g.parkedResident.recover,"job-A");
const second=f.serve();await f.idle(2);await f.stop();await second;
const a=await f.cli(["queue","collect","job-A"]),b=await f.cli(["queue","collect","job-A"]);
assert.equal(a.state,"published");assert.deepEqual(a.answer,b.answer);
const recovered=(await f.cli(["status","job-A"])).assignment;
assert.equal(recovered.status,"complete");assert.equal(recovered.submission_count,1);
assert.equal(recovered.wrapped_prompt_sha256,receipt.wrapped_prompt_sha256);
assert.deepEqual(f.s.sends,["job-A"]);
assert.deepEqual(await fs.readFile(old+"/resident-failure-final.json"),preserved);
});

test("startup recovery retains finite observation budget and sends nothing",async t=>{
const f=await fixture(t,"recoverypending");await f.open();
const c={...f.g.parkedResident.credentials,invocation:"fixture-prior",request:"job-A"};
await f.cli(["queue","submit","--request-id","job-A","--prompt-file",f.d+"/prompt.txt","--client-session-id","unit-client"]);
await f.cli(["resident","begin",J(c)]);
await f.cli(["--resident-invocation",J(c),"queue","claim","--parent-task-id",P,"--native-controls-confirmed","--request-id","job-A"]);
await f.cli(["--resident-invocation",J(c),"arm","job-A"]);
await f.cli(["resident","end",J(c)]);await f.reopen();
await assert.rejects(f.serve(),/Recovery observation ended/);
assert.deepEqual(f.s.sends,[]);assert.equal(f.s.waits,0);
assert.equal((await f.cli(["resident","inspect"])).owner.inflight,null);
assert.equal((await f.cli(["status","job-A"])).assignment.no_resend,true);
});

test("final failure preserves helper handle after detector wrote first failure",async t=>{
const f=await fixture(t),d=await f.open(),o=f.g.parkedResident;
const {recordResidentFailure}=await import(author);
await recordResidentFailure(o,{reason:"detector",pendingHelperSession:null});
const before=await fs.readFile(d+"/resident-failure.json");
await recordResidentFailure(o,{reason:"cleanup",pendingHelperSession:37,helperUncertain:true},true);
assert.deepEqual(await fs.readFile(d+"/resident-failure.json"),before);
assert.equal(JSON.parse(await fs.readFile(d+"/resident-failure-final.json")).pendingHelperSession,37);
});

for(const mode of ["beginlost","beginlostwrite"])
test("detector expiry during pending begin preserves handle and canonical reservation: "+mode,async t=>{
const f=await fixture(t,mode),d=await f.open(),serving=f.serve();
await f.idle(1);
const results=await Promise.allSettled([serving,f.start(1,"job-A")]);
assert.equal(results[0].status,"rejected");assert.deepEqual(f.s.sends,[]);
if(mode==="beginlost")assert.equal(JSON.parse(await fs.readFile(d+"/resident-failure.json")).pendingHelperSession,null);
const final=JSON.parse(await fs.readFile(d+"/resident-failure-final.json"));
assert.equal(final.pendingHelperSession,f.s.lostSession);assert.equal(final.helperUncertain,true);
if(mode==="beginlostwrite")assert.equal(final.firstRecordFailed,true);
assert.equal((await f.cli(["resident","inspect"])).owner.inflight.request,"job-A");
await assert.rejects(f.reopen(),/busy/);
});

for(const mode of ["begindrained","beginmalformed"])
test("unacknowledged begin never reports absent ownership: "+mode,async t=>{
const f=await fixture(t,mode);await f.open();const serving=f.serve();await f.idle(1);
const results=await Promise.allSettled([serving,f.start(1,"job-A")]);
assert.equal(results[0].status,"rejected");assert.deepEqual(f.s.sends,[]);
assert.equal((await f.cli(["resident","inspect"])).owner.inflight.request,"job-A");
assert.equal(f.out.find(v=>v.kind==="resident_closed").reservation_retained,"unknown");
await assert.rejects(f.reopen(),/busy/);
});

test("joined pre-arm failure resumes the same request once",async t=>{
const f=await fixture(t,"busy"),old=await f.open(),first=f.serve();
await f.idle(1);
const result=await Promise.allSettled([first,f.start(1,"job-A")]);
assert.equal(result[0].status,"rejected");assert.deepEqual(f.s.sends,[]);
assert.equal((await f.cli(["status","job-A"])).assignment.status,"prepared");
assert.equal((await f.cli(["resident","inspect"])).owner.inflight,null);
assert.notEqual(await f.reopen(),old);
assert.equal(f.g.parkedResident.recover,"job-A");
const second=f.serve();await f.idle(2);await f.stop();await second;
assert.deepEqual(f.s.sends,["job-A"]);
assert.equal((await f.cli(["queue","collect","job-A"])).state,"published");
});

test("resident accepts the incident native acknowledgment",async t=>{
const f=await fixture(t,"ack"),d=await f.open(),serving=f.serve();
await f.idle(1);await f.start(1,"job-A");await f.idle(2);
const collected=await f.cli(["queue","collect","job-A"]);
assert.equal(collected.state,"published");
assert.equal((await f.cli(["status","job-A"])).assignment.submission_count,1);
await f.stop();await serving;
assert.deepEqual(f.s.sends,["job-A"]);
assert.equal(JSON.parse(await fs.readFile(d+"/transport-audit.json")).reason,"resident_stopped");
assert.equal((await f.cli(["resident","inspect"])).owner.inflight,null);
});

for(const mode of ["claimlost","claimlostturn"])
test("lost unserved claim can be replaced without transferring a native invocation: "+mode,async t=>{
const f=await fixture(t,mode),d=await f.open();
await assert.rejects(f.serve(),/Invalid tool result/);
assert.equal(f.s.waits,0);assert.equal(f.g.parkedResident.used,true);
assert.equal((await f.cli(["resident","inspect"])).owner.inflight,null);
assert.notEqual(await f.reopen(),d);
assert.deepEqual(f.s.sends,[]);
});

test("duplicate serve with a lost reply never closes the real owner",async t=>{
const f=await fixture(t,"dupelost"),d=await f.open(),serving=f.serve();
await f.idle(1);await assert.rejects(f.serve(),/Invalid tool result/);
await missing(d+"/transport-audit.json");
await f.start(1,"job-A");await f.idle(2);await f.stop();await serving;
assert.deepEqual(f.s.sends,["job-A"]);
});

for(const mode of ["closefail","auditlost"])
test("unconfirmed close reports failure and never erases evidence: "+mode,async t=>{
const f=await fixture(t,mode),d=await f.open(),serving=f.serve();
await f.idle(1);await f.stop();await assert.rejects(serving,/Invalid tool result/);
assert.equal(f.out.find(v=>v.kind==="resident_closed").outcome,"resident_failed");
assert.equal((await f.cli(["resident","inspect"])).owner.inflight,null);
await fs.lstat(d+"/session.json");
assert.deepEqual(f.s.sends,[]);
});

// no_next_admission: B is published while A fails. No second wait, readiness,
// observed command, claim or send follows; B never enters the canonical queue.
test("failed delivery refuses the next published request",async t=>{
const f=await fixture(t,"nextqueued"),d=await f.open(),serving=f.serve();
await f.idle(1);
await Promise.allSettled([serving,f.start(1,"job-A")]);
assert.equal(f.s.waits,1);
await fs.lstat(d+"/command-2.json");
await missing(d+"/ready-2.json");await missing(d+"/command-observed-2.json");
const q=await f.cli(["queue","status"]);
assert.deepEqual(q.requests.map(r=>r.request_id),["job-A"]);
assert.equal(q.requests[0].dispatch_status,"indeterminate");
assert.deepEqual(f.s.sends,["job-A"]);
assert.equal(JSON.parse(await fs.readFile(d+"/transport-audit.json","utf8")).reason,"resident_failed");
assert.equal(f.g.parkedDelivery?.requestId,"job-A");
});


test("lost serve body recovers once with original socket, binding and canonical state",async t=>{
const f=await fixture(t),directory=await f.open();
const socket=f.g.parkedSocket,binding=f.g.parkedBinding;
const before=await f.cli(["resident","inspect"]),queue=await f.cli(["queue","status"]);
const packet=await f.recovery();
assert.deepEqual(Object.keys(packet.calls),["serve"]);
const serving=f.serve(packet.calls.serve);
await f.idle(1);
assert.equal(f.g.parkedSocket,socket);assert.equal(f.g.parkedBinding,binding);
assert.deepEqual(await f.cli(["resident","inspect"]),before);
assert.deepEqual(await f.cli(["queue","status"]),queue);
await assert.rejects(f.serve(packet.calls.serve),/proof failed/);
assert.equal(f.g.parkedSocket,socket);
await f.stop();await serving;
assert.deepEqual(f.s.sends,[]);
assert((await fs.readdir(directory)).includes("resident-serve-existing.json"));
});

for(const mismatch of [false,true])
test("truncated pre-serve packet with completed collect-only slot, changed binding: "+mismatch,async t=>{
const f=await fixture(t,"poolturn");await f.open();
const c={...f.g.parkedResident.credentials,invocation:"prior",request:"old-B",slot:"slot-1",worker:"resident-pro-b"};
await f.cli(["queue","submit","--request-id","old-B","--prompt-file",f.d+"/prompt.txt","--client-session-id","client"]);
await f.cli(["resident","begin",J(c)]);
const {createRunner}=await import(scripts+"parked-serving.mjs");
const result=await createRunner(f.tools,()=>{}).runParkedJob({...f.g.parkedSocket.config,residentInvocation:c,preflightConfirmed:true,restoreParent:false},"old-B");
assert.equal(result.ok,true);
assert.equal((await f.cli(["status","old-B"])).assignment.status,"complete");
await f.g.parkedSocket.close("resident_stopped");
const paths=(await f.cli(["status","--current"])).paths;
const proof=J({kind:"legacy_quiescence",...paths,physical_quiescence:true,implementation:"fixture",observations:"joined synthetic send",authorization:"test"});
await fs.writeFile(f.d+"/recover.json",proof,{mode:384});
await f.cli(["resident","recover-start",J({...f.g.parkedResident.credentials,new_owner:"recovered",evidence_file:f.d+"/recover.json",evidence_sha256:(await import("node:crypto")).createHash("sha256").update(proof).digest("hex")})]);
await f.reopen(); // Discard the ordinary serve body as if its output was truncated.
assert.equal(f.g.parkedResident.collectOnly,false); // An idle sibling allows ready + recovery.
assert.deepEqual(f.g.parkedResident.recovery,["old-B"]);
const before=(await f.cli(["status","old-B"])).assignment;
if(mismatch){
f.g.parkedResident.recoveryBindings["old-B"].prior_parent="foreign";
const packet=await f.recovery();
await assert.rejects(f.serve(packet.calls.serve),/recovery proof differs/);
assert.equal(f.g.parkedResident.used,true);
assert.equal(f.g.parkedResident.serveInvocation,undefined);
assert.deepEqual(f.s.sends,["old-B"]);
assert.deepEqual((await f.cli(["status","old-B"])).assignment,before);
return;
}
const packet=await f.recovery(),serving=f.serve(packet.calls.serve);
await Promise.race([f.idle(1),serving.then(()=>{throw Error("serve ended before admission");})]);
await f.stop();await serving;
assert.deepEqual(f.s.sends,["old-B"]);
assert.deepEqual((await f.cli(["status","old-B"])).assignment,before);
assert.equal((await f.cli(["resident","inspect"])).owner.slots[1].phase,"idle");
});

test("concurrent lost-body attempts have one winner",async t=>{
const f=await fixture(t);await f.open();
const a=await f.recovery(),b=await f.recovery();
let failures=0;
const running=[a,b].map(p=>f.serve(p.calls.serve).catch(e=>{assert.match(e.message,/proof failed/);failures++;}));
await f.idle(1);assert.equal(failures,1);
await f.stop();await Promise.all(running);
assert.deepEqual(f.s.sends,[]);
});

test("serve-existing rejects changed or ambiguous native identity before consuming",async t=>{
const f=await fixture(t);await f.open();const p=await f.recovery();
const o=f.g.parkedResident;
const cases=[
[f.meta,"threadId","foreign"],
[f.meta["x-codex-turn-metadata"],"turn_id",undefined],
[f.meta["x-codex-turn-metadata"],"turn_id",""],
[o.credentials,"generation",o.credentials.generation+1],
[o.credentials,"owner","foreign"],
[o,"directory",f.d],
[f.g,"parkedSocket",{...o.socket}],
[o,"socket",{...o.socket}],
[f.g,"parkedBinding",{...o.binding}],
[o,"descriptor","{}"],
[o,"used",true],[o,"used",undefined],
[o,"serveInvocation","uncertain"],[o,"serveExistingClaim","uncertain"],
[o,"serveExistingRelay",{}],[o,"unusedReplacement","uncertain"],
[o,"admission",{}],[o,"failureRecord",Promise.resolve()],
[o,"failureFinalRecord",Promise.resolve()],[o,"serveJoined",{}],
[o,"collectOnly",true],[o,"recovery",["job-old"]],
[o,"preparedRecovery",["job-old"]],[o,"recoveryBindings",{old:{}}],
[o,"recoveryBindings",[]],[o,"recoveryBindings",1],[o,"activation",{}],
[f.g,"parkedDelivery",{}],[f.g,"parkedOpenBusy",true]
];
const recover=o.recover;delete o.recover;
await assert.rejects(f.serve(p.calls.serve),/proof failed/);o.recover=recover;
for(const [obj,key,value] of cases){
const had=Object.hasOwn(obj,key),old=obj[key];obj[key]=value;
await assert.rejects(f.serve(p.calls.serve),/proof failed/);
if(had)obj[key]=old;else delete obj[key];
assert.equal(o.used,false,key);
}
const serving=f.serve(p.calls.serve);await f.idle(1);await f.stop();await serving;
});

test("ambiguous canonical claim is consumed without closing or rewriting evidence",async t=>{
const f=await fixture(t,"recoveryaudit"),directory=await f.open(),p=await f.recovery();
const before=await f.cli(["resident","inspect"]);
await fs.writeFile(directory+"/transport-audit.json","ambiguous",{mode:384});
await assert.rejects(f.serve(p.calls.serve),/prior or ambiguous evidence/);
assert.equal(f.g.parkedResident.used,true);
await assert.rejects(f.serve(p.calls.serve),/proof failed/);
assert.equal(await fs.readFile(directory+"/transport-audit.json","utf8"),"ambiguous");
assert.deepEqual(await f.cli(["resident","inspect"]),before);
// Test cleanup cannot overwrite the deliberately malformed audit.
f.g.parkedSocket.close("unit_cleanup").catch(()=>{});
});


test("lost serve-existing claim reply never enters serving and cannot retry",async t=>{
const f=await fixture(t,"serveclaimlost"),directory=await f.open(),p=await f.recovery();
f.meta["x-codex-turn-metadata"].turn_id="later-claim-turn";
const before=await f.cli(["resident","inspect"]);
await assert.rejects(f.serve(p.calls.serve),/claim uncertain/);
assert.equal(f.g.parkedResident.used,true);
assert.equal(f.g.parkedResident.serveInvocation,undefined);
assert.equal(f.g.parkedResident.admission,undefined);
assert.deepEqual((await fs.readdir(directory)).sort(),["resident-serve-existing.json","session.json","wake.sock"]);
const token=f.g.parkedResident.serveExistingClaim;
f.meta["x-codex-turn-metadata"].turn_id="still-later-turn";
await assert.rejects(f.serve(p.calls.serve),/claim uncertain/);
assert.equal(f.g.parkedResident.serveExistingClaim,token);
assert.deepEqual(await f.cli(["resident","inspect"]),before);
assert.deepEqual(f.s.sends,[]);
});

for(const mode of ["servehostempty","servehostthrow","servehostemptyafter","servehostnull","servehostmalformed","servehostfailedreceipt"]){
test(`compact claim preserves state on host failure: ${mode}`,async t=>{
const f=await fixture(t,mode),directory=await f.open(),p=await f.recovery();
const before=await f.cli(["resident","inspect"]);
await assert.rejects(f.serve(p.calls.serve),/claim uncertain/);
assert.equal(f.s.serveClaims,1,"wrapper must not retry an uncertain call");
assert.equal(f.g.parkedResident.used,mode==="servehostemptyafter");
assert.equal(f.g.parkedResident.admission,undefined);
assert.equal(f.g.parkedResident.serveInvocation,undefined);
if(mode==="servehostemptyafter")await fs.lstat(directory+"/resident-serve-existing.json");
else await missing(directory+"/resident-serve-existing.json");
assert.deepEqual(await f.cli(["resident","inspect"]),before);
assert.deepEqual(f.s.sends,[]);
});
}

test("compact claim reports foreign task as text without consuming a fence",async t=>{
const f=await fixture(t,"servehostguarderror"),directory=await f.open(),p=await f.recovery();
assert(Buffer.byteLength(p.calls.serve)<6144);
assert(!/eval\s*\(|new Function/.test(p.calls.serve));
f.meta["x-codex-turn-metadata"].turn_id="next-owner-turn";
f.meta.threadId="foreign-task";
await assert.rejects(f.serve(p.calls.serve),/proof failed/);
assert.equal(f.g.parkedResident.used,false);
assert.equal(f.g.parkedResident.serveExistingClaim,undefined);
assert.equal(f.s.suppressedNativeError,undefined,"guard error must survive a host that drops thrown errors");
await missing(directory+"/resident-serve-existing.json");
assert.deepEqual(f.s.sends,[]);
});

test("same owner later-turn recovery retains socket and canonical identity",async t=>{
const f=await fixture(t),directory=await f.open(),p=await f.recovery();
const before=await f.cli(["resident","inspect"]),socket=f.g.parkedSocket,binding=f.g.parkedBinding;
f.meta["x-codex-turn-metadata"].turn_id="later-owner-turn";
const serving=f.serve(p.calls.serve);await f.idle(1);
assert.equal(f.g.parkedSocket,socket);
assert.equal(binding.turn,"unit-turn");
assert.equal(f.g.parkedResident.binding,f.g.parkedBinding);
assert.deepEqual(f.g.parkedBinding,{broker:P,turn:"later-owner-turn"});
assert(Object.isFrozen(f.g.parkedBinding));
assert.deepEqual(await f.cli(["resident","inspect"]),before);
await fs.lstat(directory+"/resident-serve-existing.json");
await assert.rejects(f.serve(p.calls.serve),/proof failed/);
await f.stop();await serving;
assert.deepEqual(f.s.sends,[]);
});

test("compact relay survives fresh module imports on every native host call",async t=>{
const f=await fixture(t,"freshmodules");await f.open();
f.meta["x-codex-turn-metadata"].turn_id="later-owner-turn";
const p=await f.recovery(),serving=f.serve(p.calls.serve);
await f.idle(1);assert(f.s.repls>3);
await f.stop();await serving;assert.deepEqual(f.s.sends,[]);
});

test("compact pool dispatch is not queued behind an idle sibling on a serialized native host",async t=>{
const f=await fixture(t,"poolturn");await f.open();
const native=f.tools.mcp__node_repl__js;
let tail=Promise.resolve();
const batches=[];
// Real native REPL calls share one execution lane. The old fixture ran them
// concurrently and hid the 25-second admission wait before each evidence call.
f.tools.mcp__node_repl__js=args=>{
const result=tail.then(async()=>{
if(args.title==="Preserve native evidence"&&args.code.includes("const second="))batches.push(args.code);
return native(args);
});

tail=result.catch(()=>{});return result;
};
const packet=await f.recovery(),serving=f.serve(packet.calls.serve);
await f.idle(1);
let timer;
try{
const result=await Promise.race([
f.start(1,"latency-A"),
new Promise((_,reject)=>{timer=setTimeout(()=>reject(Error("dispatch blocked by idle admission")),15000);})
]);
assert.equal(result.ok,true);
assert.deepEqual(f.s.sends,["latency-A"]);
assert.equal(batches.length,2,"pre-send and post-send reads each persist one evidence batch");
for(const code of batches){
const path=JSON.parse(/const path=(.*);/.exec(code)[1]);
const rawPath=JSON.parse(/const second=await fs.open\((.*),"wx",0o600\)/.exec(code)[1]);
const envelope=JSON.parse(await fs.readFile(path,"utf8"));
assert.equal(await fs.readFile(rawPath,"utf8"),envelope.result.content[0].text);
assert.equal((await fs.stat(path)).mode&0o777,0o600);
assert.equal((await fs.stat(rawPath)).mode&0o777,0o600);
}
}finally{clearTimeout(timer);await f.stop();await serving;await tail;}
});

test("serialized host retains sibling admission between calls and assigns B before A completes",async t=>{
const f=await fixture(t,"poolturn");await f.open();
const native=f.tools.mcp__node_repl__js,read=f.tools.mcp__codex_app__read_thread;
let tail=Promise.resolve(),releaseA,observedSlice;
const heldA=new Promise(resolve=>{releaseA=resolve;});
const slice=new Promise(resolve=>{observedSlice=resolve;});
let aFinished=false;
f.tools.mcp__codex_app__read_thread=async args=>{
if(args.threadId===W&&f.s.poolSent?.has(W))await heldA;
return read(args);
};
f.tools.mcp__node_repl__js=args=>{
const result=tail.then(async()=>{
const value=await native(args);
if(args.code.includes("activation.residentAdmission(")&&args.code.includes(",undefined,true")){
// Pause the serialized lane after the bounded observation returned. Clients
// must still be able to claim readiness while other native work owns the lane.
observedSlice();await pause(600);
}
return value;
});
tail=result.catch(()=>{});return result;
};
const packet=await f.recovery(),serving=f.serve(packet.calls.serve);
serving.catch(()=>{});
let clientA,clientB;
try{
await f.idle(1);
clientA=f.start(1,"overlap-A").then(v=>{aFinished=true;return v;});clientA.catch(()=>{});
await Promise.race([slice,pause(10000).then(()=>{throw Error("no active admission slice");})]);
// Fails deterministically with the old 250ms retire/reopen implementation.
await fs.lstat(f.waiting(2));
clientB=f.start(2,"overlap-B");clientB.catch(()=>{});
assert.equal((await clientB).ok,true);
assert.equal(aFinished,false,"B must finish while A remains held");
const a=(await f.cli(["status","overlap-A"])).assignment;
const b=(await f.cli(["status","overlap-B"])).assignment;
assert.equal(a.worker_slot,"slot-0");assert.equal(b.worker_slot,"slot-1");
assert.deepEqual(f.s.sends,["overlap-A","overlap-B"]);
}finally{
releaseA();await Promise.allSettled([clientA,clientB]);
await f.stop();await serving;await tail;
}
});

test("incomplete evidence batch never arms or sends",async t=>{
const f=await fixture(t,"evidencepartial");await f.open();
const serving=f.serve();await f.idle(1);
const [served]=await Promise.allSettled([serving,f.start(1,"partial-evidence")]);
assert.equal(served.status,"rejected");
assert(f.s.evidenceError);
assert.deepEqual(f.s.sends,[]);
assert.equal((await f.cli(["status","partial-evidence"])).assignment.status,"prepared");
});

test("pooled evidence validates owner in the write call before arming",async t=>{
const f=await fixture(t,"poolturn");await f.open();
const native=f.tools.mcp__node_repl__js;
let changed=false;
f.tools.mcp__node_repl__js=async args=>{
if(!changed&&args.title==="Preserve native evidence"){
changed=true;f.g.parkedBinding=Object.freeze({...f.g.parkedBinding});
}
return native(args);
};
const packet=await f.recovery(),serving=f.serve(packet.calls.serve);
await f.idle(1);
const [served]=await Promise.allSettled([serving,f.start(1,"changed-owner")]);
assert.equal(served.status,"rejected");assert(changed);
assert.deepEqual(f.s.sends,[]);
assert.equal((await f.cli(["status","changed-owner"])).assignment.status,"prepared");
});

for(const scenario of ["retained","delayed local continuation","legacy queued","canonical changed","missing legacy call"])
test("two armed pool calls across turn end: "+scenario,async t=>{
const f=await fixture(t,"poolturn");await f.open();
const a=await import(author),o=f.g.parkedResident;
const current=(await f.cli(["resident","inspect"])).owner;
const expected={generation:current.generation,owner:current.owner,parent:current.parent,
worker_pool_sha256:current.worker_pool_sha256,session:current.session};
await a.claimServeExisting(f.g,f.meta,expected,"original-relay");
let token="original-relay",step=await a.serveExistingStep(f.g,f.meta,token,null);
const pending=new Map(),held=[];
const clientA=waitForFile(f.waiting(1)).then(()=>f.start(1,"job-A"));
const clientB=waitForFile(f.waiting(2)).then(()=>f.start(2,"job-B"));
clientA.catch(()=>{});clientB.catch(()=>{});
// Execute the real serving loop/CLI against an isolated authority, substituting
// only native ChatGPT responses. Lose the outer driver at the two owner checks.
async function tick(hold){
for(const call of step.calls){
if(hold&&f.s.armChecksToHold&&call.tool==="mcp__node_repl__js"&&
call.args.code.endsWith('console.log("{}");}')){
f.s.armChecksToHold--;a.claimRelayCall(f.g,f.meta,token,call.id);held.push(call);continue;
}
const claimed=a.claimRelayCall(f.g,f.meta,token,call.id);
pending.set(call.id,f.tools[claimed.tool](claimed.args).then(value=>({id:call.id,value}),e=>({id:call.id,error:e.message})));
}
if(held.length===2&&hold)return;
assert(pending.size,"serving loop unexpectedly stalled");
const reply=await Promise.race(pending.values());pending.delete(reply.id);
if(!hold&&scenario==="delayed local continuation"){
// Force a gap with no host calls while the live serving promise continues
// asynchronously. One setImmediate is not a completion barrier.
const entry=o.serveExistingRelay.pending.get(reply.id),resolve=entry.resolve;
entry.resolve=value=>{setTimeout(()=>resolve(value),30);};
}
step=await a.serveExistingStep(f.g,f.meta,token,reply);
if(!hold&&scenario==="delayed local continuation")
assert(step.done||step.error||step.calls.length||pending.size,"live relay was reported stalled");
f.out.push(...step.outputs);
if(step.error)throw Error(step.error+" "+f.s.failureEvidence+" "+J(f.out));
}
for(let i=0;i<250&&held.length<2;i++)await tick(true);
assert.equal(held.length,2);assert.equal(pending.size,0);
const before=await f.cli(["resident","inspect"]);
for(const rid of ["job-A","job-B"]){const receipt=(await f.cli(["status",rid])).assignment;assert.equal(receipt.status,"armed");assert.equal(receipt.submission_count,0);}
assert.deepEqual(f.s.sends,[]);
const invocation=o.serveInvocation,relay=o.serveExistingRelay,running=relay.running;
const originalBinding=o.binding;
f.meta["x-codex-turn-metadata"].turn_id="later-turn";
await assert.rejects(a.serveExistingStep(f.g,f.meta,token,{id:held[0].id,value:mcp({})}),/identity changed/);
if(scenario==="canonical changed"){
expected.generation++;o.credentials.generation++;
await assert.rejects(a.continueServeExisting(f.g,f.meta,expected,"blocked"),/owner replaced/);
assert.equal(relay.continuing,"blocked");assert.equal(o.binding,originalBinding);
assert.deepEqual(await f.cli(["resident","inspect"]),before);assert.deepEqual(f.s.sends,[]);
return;
}
if(scenario.includes("legacy")){
for(const call of relay.pending.values()){delete call.request;delete call.dispatched;}
relay.calls=scenario==="legacy queued"?[...held]:[];
if(scenario==="missing legacy call"){
await assert.rejects(a.continueServeExisting(f.g,f.meta,expected,"blocked"),/collect-only recovery required/);
assert.equal(o.binding,originalBinding);assert.deepEqual(f.s.sends,[]);return;
}
}
// Native identity and exact pending-call guards must reject without consuming
// the continuation claim or mutating canonical request evidence.
for(const [object,key,value] of [[f.meta,"threadId","foreign"],[o.credentials,"generation",999],
[o,"socket",{}],[o,"failureRecord",{}],[relay,"done",true]]){
const original=object[key];object[key]=value;
await assert.rejects(a.continueServeExisting(f.g,f.meta,expected,"invalid"),/proof failed/);
if(original===undefined)delete object[key];else object[key]=original;
assert.equal(relay.continuing,undefined);
}
const request=scenario==="legacy queued"?relay.calls[0]:relay.pending.get(held[0].id).request;
const tool=request.tool;request.tool="mcp__codex_app__send_message_to_thread";
await assert.rejects(a.continueServeExisting(f.g,f.meta,expected,"lost-send"),/collect-only recovery required/);
request.tool=tool;
assert.deepEqual(await a.continueServeExisting(f.g,f.meta,expected,"continued-relay"),{claimed:true,continued:true});
assert.equal(o.serveInvocation,invocation);assert.equal(o.serveExistingRelay,relay);assert.equal(relay.running,running);
assert.deepEqual(await f.cli(["resident","inspect"]),before);
await assert.rejects(a.continueServeExisting(f.g,f.meta,expected,"duplicate"),/proof failed/);
await assert.rejects(a.continueServeExisting(f.g,{threadId:P,"x-codex-turn-metadata":{turn_id:originalBinding.turn}},expected,"old-driver"),/proof failed/);
assert.throws(()=>a.claimRelayCall(f.g,{threadId:P,"x-codex-turn-metadata":{turn_id:originalBinding.turn}},token,held[0].id),/identity changed/);
await assert.rejects(a.serveExistingStep(f.g,f.meta,token,{id:held[0].id,value:mcp({})}),/identity changed/);
token="continued-relay";step=await a.serveExistingStep(f.g,f.meta,token,null);
let recoveryReady;
if(scenario==="delayed local continuation"){
recoveryReady=Promise.all([clientA,clientB]).then(async()=>{
await waitForFile(f.waiting(3));
await f.stop();
});
}else await f.stop();
for(let i=0;i<300&&!step.done;i++)await tick(false);
assert(step.done);await Promise.all([clientA,clientB]);
await recoveryReady;
if(scenario==="delayed local continuation"){
const owner=(await f.cli(["resident","inspect"])).owner;
assert(owner.slots.every(slot=>slot.request===null&&slot.invocation===null),J(owner.slots));
}
assert.deepEqual(f.s.sends.sort(),["job-A","job-B"]);
for(const rid of ["job-A","job-B"]){const receipt=(await f.cli(["status",rid])).assignment;assert.equal(receipt.submission_count,1);assert.equal(receipt.status,"complete");}
assert.equal(relay.pending.size,0);
assert.throws(()=>a.claimRelayCall(f.g,f.meta,token,held[0].id),/consumed or unknown/);
});

test("first relay identity rejection preserves its exact diagnostic",async t=>{
const f=await fixture(t,"relayidentity");await f.open();const p=await f.recovery();
await assert.rejects(f.serve(p.calls.serve),/Serve-existing relay identity changed; preserve claim/);
assert.equal(f.g.parkedResident.used,true);
assert.equal(f.g.parkedResident.admission,undefined);
assert.deepEqual(f.s.sends,[]);
});

for(const mode of ["ok","legacyrelay","replacementlost"]){
test(`unused consumed replacement preserves evidence and never replays: ${mode}`,async t=>{
const f=await fixture(t,mode),directory=await f.open();
const owner=(await f.cli(["resident","inspect"])).owner;
const expected={generation:owner.generation,owner:owner.owner,parent:owner.parent,
worker:owner.worker,session:owner.session};
const activation=await import(author);
await activation.claimServeExisting(f.g,f.meta,expected,"consumed-unused");
if(mode==="legacyrelay")delete f.g.parkedResident.serveExistingRelay;
const evidence=await fs.readFile(directory+"/resident-serve-existing.json");
const descriptor=await fs.readFile(directory+"/session.json");
const packet=await f.activate(["resident-replace-unused-packet",directory]);
if(mode==="replacementlost")await assert.rejects(f.serve(packet.calls.replace),/replacement uncertain/);
else await f.serve(packet.calls.replace);
assert.deepEqual(await fs.readFile(directory+"/resident-serve-existing.json"),evidence);
assert.deepEqual(await fs.readFile(directory+"/session.json"),descriptor);
await fs.lstat(directory+"/resident-unused-replacement.json");
await assert.rejects(f.serve(packet.calls.replace),/proof failed|uncertain/);
await assert.rejects(activation.serveExistingStep(f.g,f.meta,"consumed-unused",null),/identity changed/);
if(mode!=="replacementlost"){
const fresh=await f.reopen();assert.notEqual(fresh,directory);
const p=await f.recovery(),serving=f.serve(p.calls.serve);
await f.idle(1);await f.stop();await serving;
}
assert.deepEqual(f.s.sends,[]);
});
}

test("unused replacement rejects foreign, changed, non-pristine and started native state",async t=>{
const f=await fixture(t),directory=await f.open();
const owner=(await f.cli(["resident","inspect"])).owner;
const expected={generation:owner.generation,owner:owner.owner,parent:owner.parent,worker:owner.worker,session:owner.session};
const a=await import(author),o=f.g.parkedResident;
await a.claimServeExisting(f.g,f.meta,expected,"unused");
for(const [obj,key,value] of [[f.meta,"threadId","foreign"],
[f.g,"parkedSocket",{...o.socket}],[o.credentials,"generation",999],
[o,"admission",{}],[o,"failureRecord",{}],[o,"serveInvocation","active"],
[o,"recovery",["request"]],[o.serveExistingRelay,"started",true]]){
const prior=obj[key];obj[key]=value;
await assert.rejects(a.replaceUnusedServing(f.g,f.meta,expected,"replacement"),/proof failed/);
if(prior===undefined)delete obj[key];else obj[key]=prior;
assert.equal(o.unusedReplacement,undefined);
await missing(directory+"/resident-unused-replacement.json");
}
assert.deepEqual(f.s.sends,[]);
});


test("compact recovery packet stays well below the 20000-character host ceiling",async t=>{
const f=await fixture(t,"replacementtext"),directory=await f.open();
const before=await f.cli(["resident","inspect"]),descriptor=await fs.readFile(directory+"/session.json");
const first=await f.recovery(),second=await f.recovery();
t.diagnostic(`packet=${Buffer.byteLength(J(second))+1} bytes; calls.serve=${Buffer.byteLength(second.calls.serve)} bytes`);
for(const packet of [first,second]){
assert(Buffer.byteLength(J(packet))+1<8192,"entire CLI output must fit 8 KiB");
assert(Buffer.byteLength(packet.calls.serve)<6144,"serve wrapper must fit 6 KiB");
assert(!/eval\s*\(|new Function|runParkedJob/.test(packet.calls.serve));
}
assert.deepEqual(await f.cli(["resident","inspect"]),before);
assert.deepEqual(await fs.readFile(directory+"/session.json"),descriptor);
assert.deepEqual((await fs.readdir(directory)).sort(),["session.json","wake.sock"]);
assert.equal(f.g.parkedResident.used,false);
// Discarded output generated no claim. The replacement may serve once.
const serving=f.serve(second.calls.serve);await f.idle(1);
await assert.rejects(f.serve(first.calls.serve),/proof failed/);
await f.start(1,"job-A");await f.idle(2);await f.stop();await serving;
assert.deepEqual(f.s.sends,["job-A"]);
assert.equal((await f.cli(["status","job-A"])).assignment.submission_count,1);
});

test("compact wrapper rejects a stale activation pin before claiming",async t=>{
const f=await fixture(t),directory=await f.open(),p=await f.recovery();
const hash=(await import("node:crypto")).createHash("sha256").update(await fs.readFile(author)).digest("hex");
await assert.rejects(f.serve(p.calls.serve.replaceAll(hash,"0".repeat(64))),/Activation pin changed/);
assert.equal(f.g.parkedResident.used,false);
await missing(directory+"/resident-serve-existing.json");
});

test("lost relay reply cannot restart a claimed continuation",async t=>{
const f=await fixture(t,"relaylost"),directory=await f.open(),p=await f.recovery();
await assert.rejects(f.serve(p.calls.serve),/relay uncertain/);
assert.equal(f.g.parkedResident.used,true);
await fs.lstat(directory+"/resident-serve-existing.json");
const hash=(await import("node:crypto")).createHash("sha256").update(await fs.readFile(author)).digest("hex");
const activation=await import(author+"?sha256="+hash),token=f.g.parkedResident.serveExistingClaim;
await assert.rejects(activation.serveExistingStep(f.g,f.meta,token,null),/Unknown or consumed/);
await assert.rejects(activation.serveExistingStep(f.g,f.meta,token,{id:999,value:{}}),/Unknown or consumed/);
await assert.rejects(activation.serveExistingStep(f.g,f.meta,"other-token",{id:1,value:{}}),/identity changed/);
await assert.rejects(activation.serveExistingStep(f.g,{...f.meta,threadId:"foreign"},token,{id:1,value:{}}),/identity changed/);
await assert.rejects(activation.serveExistingStep(f.g,{...f.meta,"x-codex-turn-metadata":{turn_id:"foreign"}},token,{id:1,value:{}}),/identity changed/);
await assert.rejects(f.serve((await f.recovery()).calls.serve),/proof failed/);
assert.deepEqual(f.s.sends,[]);
});

test("static runner factory matches the immutable pinned runner",async()=>{
const {createRunner}=await import(scripts+"parked-serving.mjs");
const raw=await fs.readFile(scripts+"parked-runner.js","utf8");
const expected="function createRunner(tools,text){\n"+raw.replace(/globalThis\.(describeFailure|runParkedJob|runParkedDelivery|runParkedWorkerPool) =/g,"const $1 =").replace("globalThis.runParkedPool = globalThis.runParkedWorkerPool;","return {describeFailure,runParkedJob,runParkedDelivery,runParkedWorkerPool};")+"\n}";
assert.equal(createRunner.toString(),expected);
});

test("activation rejects modified serving module bytes",async()=>{
const original=nativeFs.readFile;
nativeFs.readFile=async function(path,...args){
const raw=await original.call(this,path,...args);
return String(path).endsWith("/parked-serving.mjs")?Buffer.concat([raw,Buffer.from("\n// changed")]):raw;
};
syncBuiltinESMExports();
try{await assert.rejects(import(author+"?bad-serving-pin"),/Pinned serving module mismatch/);}
finally{nativeFs.readFile=original;syncBuiltinESMExports();}
});
