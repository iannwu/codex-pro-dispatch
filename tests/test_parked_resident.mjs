import test from "node:test";
import assert from "node:assert/strict";
import * as fs from "node:fs/promises";
import {tmpdir} from "node:os";
import {execFile} from "node:child_process";
import {EventEmitter} from "node:events";
import {fileURLToPath} from "node:url";

const root=fileURLToPath(new URL("../",import.meta.url));
const scripts=root+"skills/codex-pro-dispatch/scripts/",author=scripts+"parked-activation.mjs";
const P="resident-parent",W="resident-pro",J=JSON.stringify;
const AF=Object.getPrototypeOf(async function(){}).constructor;
const pause=ms=>new Promise(r=>setTimeout(r,ms));
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
await g.parkedSocket?.close("unit_cleanup");
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
const access=await activate(["client-preflight",d]);
assert.equal(access.filesystemAccess,true);assert.equal(access.sendAuthorized,false);
assert(!(await fs.readdir(d)).some(n=>n.startsWith(".pro-access-")));
let p=await activate(["resident-packet",P,P,W,d]);
const prompt=d+"/prompt.txt";await fs.writeFile(prompt,"Fixture answer.",{mode:384});
const tools={
async mcp__node_repl__js(a){
s.repls++;
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
await new AF("nodeRepl","globalThis","console","parkedSocket","parkedBinding",a.code)(
{tmpDir:d,requestMeta:meta},g,{log:v=>lines.push(String(v))},
g.parkedSocket,g.parkedBinding);
return {content:[{type:"text",text:lines.join("\n")}]};
},
async exec_command(a){
// Force a nonterminal observation past the budget, without real hour-long waits.
if(mode==="pending"&&a.cmd.includes("'queue' 'observe'")){
s.tick+=3600001;
return {exit_code:0,output:J({ok:true,request_id:last.id,parent_task_id:P,
worker_conversation_id:W,observation:"pending"})};
}
const pending=run("/bin/sh",["-c",a.cmd]);
if(a.cmd.includes("'resident-next'")){
const id=++serial;sessions.set(id,pending);s.waits++;events.emit("change");
return {session_id:id,output:""};
}
if(mode==="helperlost"&&a.cmd.includes("'command-ready'")){
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
if(a.cmd.includes("'arm'")){
if(mode==="turn")meta["x-codex-turn-metadata"].turn_id="changed";
if(mode==="owner")g.parkedBinding=Object.freeze({...g.parkedBinding});
}
return result;
},
async write_stdin(a){
assert.equal(a.chars,"");assert(sessions.has(a.session_id));
if(a.session_id===s.lostSession){s.drains=(s.drains||0)+1;throw Error(s.drains===1?"host lost":"host lost again");}
if(a.session_id===s.stuckSession){s.drains=(s.drains||0)+1;return {session_id:a.session_id,output:""};}
const r=await sessions.get(a.session_id);sessions.delete(a.session_id);return r;
},
async mcp__codex_app__read_thread(a){
assert.equal(a.threadId,W);s.reads++;
if(mode==="stoprace"&&last)await stop(); // Explicit stop racing the failure below.
if(mode==="nextqueued"&&last)await publishB(); // B published while A is failing.
if(mode==="externalclose"&&last)await g.parkedSocket.close("unit_external_close");
if(["blocked","stoprace","nextqueued","externalclose","blockedclosefail"].includes(mode)&&last)return {isError:true};
return mcp({schemaVersion:1,thread:{id:W,kind:"chatgpt",status:{type:"idle"}},
turns:last?[{id:"turn-"+last.id,items:[
{id:"turn-"+last.id,type:"userMessage",content:[{type:"text",text:last.prompt}]},
{id:"answer-"+last.id,type:"agentMessage",text:
"[CODEX_PRO_DISPATCH_RESULT assignment_id="+last.id+"]\nanswer\n"+
"[CODEX_PRO_DISPATCH_END assignment_id="+last.id+"]"}]}]:[]});
},
async mcp__codex_app__send_message_to_thread(a){
assert.equal(a.threadId,W);
const id=/^\[CODEX_PRO_DISPATCH assignment_id=([^\]]+)\]\n/.exec(a.prompt)?.[1];
assert(id);last={id,prompt:a.prompt};s.sends.push(id);
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
await new AF("tools","text",p.calls.open)(tools,()=>{});
return g.parkedResident.directory;
}
const Clock=class extends Date{static now(){return Date.now()+s.tick;}};
const serve=()=>track(new AF("tools","text","Date",p.calls.serve)(
tools,v=>out.push(v),Clock));
const start=(n,id)=>activate(["rendezvous",g.parkedResident.directory,
String(n),id,prompt,"unit-client"]);
const stop=()=>activate(["resident-stop",g.parkedResident.directory]);
const repeat=id=>json(process.execPath,[scripts+"parked-client.mjs",
g.parkedResident.directory,"submit",id,prompt,"unit-client"]);
const reopen=async(action="closed-resident-packet",dir=g.parkedResident.directory)=>{
p=await activate([action,P,P,W,dir,d]);
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
return {g,meta,s,out,cli,open,serve,start,stop,repeat,reopen,activate,d,children,waiting,publish,
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

test("interrupted unserved owner cancels and reopens without sending",async t=>{
const f=await fixture(t),old=await f.open();
const {cancelUnstartedResident}=await import(author);
await assert.rejects(cancelUnstartedResident(f.g,f.meta),/interrupted/);
f.meta["x-codex-turn-metadata"].turn_id="next-turn";
assert.equal((await cancelUnstartedResident(f.g,f.meta)).reason,"resident_start_cancelled");
await missing(old+"/wake.sock");
const fresh=await f.reopen();assert.notEqual(fresh,old);
const serving=f.serve();await f.idle(1);await f.stop();await serving;
assert.deepEqual(f.s.sends,[]);
});

for(const blocker of ["used","held","foreign","ready","queued"])
test("interrupted recovery rejects "+blocker,async t=>{
const f=await fixture(t),d=await f.open();
const {cancelUnstartedResident}=await import(author);
f.meta["x-codex-turn-metadata"].turn_id="next-turn";
if(blocker==="used")f.g.parkedResident.used=true;
if(blocker==="held")f.g.parkedDelivery={requestId:"unknown"};
if(blocker==="foreign")f.meta.threadId="other-parent";
if(blocker==="ready")await fs.writeFile(d+"/ready-1.json","{}",{mode:384});
if(blocker==="queued"){
await fs.mkdir(d+"/command-1",{mode:448});
const prompt="Not to send",{createHash}=await import("node:crypto");
await fs.writeFile(d+"/command-1/prompt.txt",prompt,{mode:384});
await fs.writeFile(d+"/command-1.json",J({sessionId:f.g.parkedSocket.config.sessionId,
ordinal:1,requestId:"queued",deadlineAt:1,promptSha256:createHash("sha256").update(prompt).digest("hex")}),{mode:384});
await f.cli(["queue","submit","--request-id","queued","--prompt-file",d+"/command-1/prompt.txt","--client-session-id","unit-client"]);
}
await assert.rejects(cancelUnstartedResident(f.g,f.meta));
await fs.lstat(d+"/wake.sock");await missing(d+"/transport-audit.json");
assert.deepEqual(f.s.sends,[]);
});

test("expired unobserved request remains evidence, never resubmitted",async t=>{
const f=await fixture(t),d=await f.open();
const {cancelUnstartedResident}=await import(author),{createHash}=await import("node:crypto");
const prompt="Cancelled",raw=J({sessionId:f.g.parkedSocket.config.sessionId,
ordinal:1,requestId:"cancelled",deadlineAt:1,promptSha256:createHash("sha256").update(prompt).digest("hex")});
await fs.mkdir(d+"/command-1",{mode:448});
await fs.writeFile(d+"/command-1/prompt.txt",prompt,{mode:384});
await fs.writeFile(d+"/command-1.json",raw,{mode:384});
f.meta["x-codex-turn-metadata"].turn_id="next-turn";
await cancelUnstartedResident(f.g,f.meta);
assert.notEqual(await f.reopen(),d);
assert.equal(await fs.readFile(d+"/command-1.json","utf8"),raw);
assert.deepEqual((await f.cli(["queue","status"])).requests,[]);
assert.deepEqual(f.s.sends,[]);
});

for(const ids of [[],["job-A","job-B"]])
test("resident unknown-request cycle: "+ids.join(","),async t=>{
const f=await fixture(t),d=await f.open();
await assert.rejects(f.open());
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
assert.equal(JSON.parse(await fs.readFile(old+"/replacement-open.once.json","utf8")).previousSessionId,
JSON.parse(await fs.readFile(old+"/session.json","utf8")).sessionId);
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

// A waiter that crashed leaves a marker whose heartbeat stopped; stale
// readiness fails closed and nothing is published toward the dead waiter.
test("stale readiness after a crashed waiter refuses to publish",async t=>{
const f=await fixture(t),d=await f.open();
const waiting=f.activate(["resident-next",d,"1"]).catch(e=>e);
const marker=f.waiting(1);
for(let i=0;i<200;i++){try{await fs.lstat(marker);break;}catch{await pause(25);}}
const waiter=[...f.children].find(c=>c.spawnargs.includes("resident-next"));
waiter.kill("SIGKILL");await waiting;
assert((await fs.lstat(marker)).isDirectory());
// Heartbeat stopped with the process; once stale the client fails closed.
const stale=new Date(Date.now()-6000);await fs.utimes(marker,stale,stale);
await assert.rejects(f.start(1,"job-A"),/Stale resident readiness for ordinal 1; do not publish/);
await missing(d+"/command-1.json");await missing(d+"/command-1");
assert.deepEqual((await f.cli(["queue","status"])).requests,[]);
// The crash evidence stays; a later serve cannot silently take the ordinal.
await fs.lstat(marker);
await assert.rejects(f.serve(),/Helper failed/);
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
// Child-only barrier inside the client: after its freshness read, before its claim.
await fs.writeFile(hook,`
const fs=require("node:fs/promises"),{existsSync}=require("node:fs");
const rename=fs.rename;
fs.rename=async function(from,to){
if(from===${J(f.waiting(1))}){
await fs.writeFile(${J(paused)},"");
const limit=Date.now()+20000;
while(!existsSync(${J(release)})){
if(Date.now()>limit)throw Error("Fixture barrier timed out");
await new Promise(r=>setTimeout(r,10));
}
}
return rename(from,to);
};
require("node:module").syncBuiltinESMExports();
`);
const client=assert.rejects(f.activate(["rendezvous",old,"1","job-A",f.d+"/prompt.txt","unit-client"],
["--require",hook]),/not waiting for ordinal 1; do not publish/);
for(let i=0;;i++){try{await fs.lstat(paused);break;}catch{if(i>=400)throw Error("Client never paused");await pause(25);}}
// The waiter returns while the client is paused, so retirement wins the race.
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
await assert.rejects(serving,/Helper failed/);
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
await assert.rejects(serving,/Helper failed/);
assert(Date.now()-at<15000);
assert.equal(f.out.find(v=>v.kind==="resident_closed")?.outcome,"resident_failed");
assert.deepEqual(await fs.readdir(d+"/command-1"),[]);
await missing(d+"/command-1.json");await missing(d+"/command-observed-1.json");
await missing(d+"/ready-1.json");await missing(d+"/resident-stop.json");
assert.deepEqual((await f.cli(["queue","status"])).requests,[]);
// No replay: the closed resident refuses clients, and the claimed ticket
// blocks replacement while staying exactly as it was.
await assert.rejects(f.start(1,"job-A"),/Existing rendezvous artifact/);
await assert.rejects(f.reopen());
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
await assert.rejects(serving,/Helper failed/);
const failure=JSON.parse(await fs.readFile(old+"/resident-failure.json","utf8"));
assert.equal(failure.requestId,null);assert.equal(failure.heldDelivery,null);
assert.equal(failure.stopRequested,false);
assert.equal(JSON.parse(await fs.readFile(old+"/transport-audit.json","utf8")).reason,"resident_failed");
await missing(old+"/wake.sock");
return {f,old,failure};
}

test("failed-resident-packet replaces a zero-send failed residence once, in the same task",async t=>{
const {f,old,failure}=await failedResidence(t);
const before=await fs.readFile(old+"/command-1.json");
// The ordinary replacement and cancellation paths still refuse this closure.
await assert.rejects(f.reopen(),/not a matching closed/);
const {cancelUnstartedResident}=await import(author);
await assert.rejects(cancelUnstartedResident(f.g,f.meta),/Not an interrupted unserved owner/);
await missing(old+"/replacement-open.once.json");
const fresh=await f.reopen("failed-resident-packet");
assert.notEqual(fresh,old);
// Old evidence is intact and the replacement marker is consumed exactly once.
assert.deepEqual(await fs.readFile(old+"/command-1.json"),before);
assert.deepEqual(JSON.parse(await fs.readFile(old+"/resident-failure.json","utf8")),failure);
assert.equal(JSON.parse(await fs.readFile(old+"/replacement-open.once.json","utf8")).previousSessionId,failure.sessionId);
await assert.rejects(f.reopen("failed-resident-packet",old),/Unproven resident artifacts/);
// The fresh residence serves new work; the unobserved request is never replayed.
const second=f.serve();await f.idle(2);
await f.start(1,"job-B");await f.idle(3);
assert.equal((await f.cli(["queue","collect","job-B"])).state,"published");
await f.stop();await second;
assert.deepEqual(f.s.sends,["job-B"]);
assert.deepEqual((await f.cli(["queue","status"])).requests.map(r=>r.request_id),["job-B"]);
});

test("failed-resident-packet refuses any retained request, delivery, event, artifact or authority change",async t=>{
const {f,old,failure}=await failedResidence(t);
const failed=()=>f.reopen("failed-resident-packet");
const failureFile=old+"/resident-failure.json",rewrite=v=>fs.writeFile(failureFile,J(v),{mode:384});
for(const [name,patch] of [["requestId","job-A"],["heldDelivery",{requestId:"job-A"}],["pendingHelperSession",7]]){
await rewrite({...failure,[name]:patch});
await assert.rejects(failed(),/retained a request, delivery or helper/);
}
await rewrite({...failure,extra:true});
await assert.rejects(failed(),/retained a request, delivery or helper/);
await rewrite(failure);
for(const name of ["command-observed-1.json","ready-1.json","stray.json"]){
await fs.writeFile(old+"/"+name,"{}",{mode:384});
await assert.rejects(failed(),/Unproven resident artifacts/);
await fs.rm(old+"/"+name);
}
// A publication that has not expired is not proven unobservable yet.
const command=await fs.readFile(old+"/command-1.json");
await fs.writeFile(old+"/command-1.json",J({...JSON.parse(command),deadlineAt:Date.now()+60000}),{mode:384});
await assert.rejects(failed(),/Unproven resident artifacts/);
await fs.writeFile(old+"/command-1.json",command,{mode:384});
// Retained runtime state and trusted authority must match exactly.
f.g.parkedDelivery={requestId:"uncertain"};
await assert.rejects(failed(),/not recoverable/);
f.g.parkedDelivery=null;
const owner=f.g.parkedResident;
f.g.parkedResident={...owner,used:false};
await assert.rejects(failed(),/Owner exists/);
f.g.parkedResident=owner;
const setWorker=(id,expected)=>f.cli(["worker","set","--conversation-id",id,"--expected-conversation-id",expected,"--confirm-pro","--native-controls-confirmed"]);
await setWorker("other-pro",W);
await assert.rejects(failed(),/Production worker mismatch/);
await setWorker(W,"other-pro");
await assert.rejects(f.activate(["failed-resident-packet","other-task","other-task",W,old,f.d]),/not a matching closed/);
// The unobserved request entering the canonical queue blocks replacement for good.
await f.cli(["queue","submit","--request-id","job-A","--prompt-file",f.d+"/prompt.txt","--client-session-id","unit-client"]);
await assert.rejects(failed(),/entered queue/);
await missing(old+"/replacement-open.once.json");
assert.deepEqual(f.s.sends,[]);
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

test("unacknowledged answer prevents closed resident replacement",async t=>{
const f=await fixture(t);await f.open();const serving=f.serve();
await f.idle(1);await f.start(1,"job-A");await f.idle(2);
await f.stop();await serving;
await assert.rejects(f.reopen(),/Canonical terminal receipt mismatch/);
assert.deepEqual(f.s.sends,["job-A"]);
});

test("unexpected command evidence prevents unused resident replacement",async t=>{
const f=await fixture(t),d=await f.open(),serving=f.serve();
await f.idle(1);await f.stop();await serving;
await fs.writeFile(d+"/command-1.json","{}",{mode:384});
await assert.rejects(f.reopen(),/Unproven resident artifacts/);
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

test("held delivery or changed retained owner blocks restart before marker",async t=>{
const f=await fixture(t),d=await f.open(),serving=f.serve();
await f.idle(1);await f.stop();await serving;
f.g.parkedDelivery={requestId:"uncertain"};
await assert.rejects(f.reopen(),/not recoverable/);
await missing(d+"/replacement-open.once.json");
f.g.parkedDelivery=null;
f.g.parkedResident={...f.g.parkedResident,used:false};
await assert.rejects(f.reopen(),/Owner exists/);
await missing(d+"/replacement-open.once.json");
});

// exit_label_matrix: every unresolved outcome fails the residence; only the
// explicit stop above closes as resident_stopped. Delivery stays held.
for(const mode of ["pending","blocked","owner","turn","decoder","transport","ackworker","stoprace","externalclose"])
test("resident fails and retains delivery: "+mode,async t=>{
const f=await fixture(t,mode),d=await f.open(),serving=f.serve();
await f.idle(1);
const [served]=await Promise.allSettled([serving,f.start(1,"job-A")]);
assert.equal(served.status,"rejected");
assert.equal(f.s.waits,1);
assert.equal(f.g.parkedDelivery?.requestId,"job-A");
const receipt=f.out.find(v=>v.kind==="runner_receipt");
if(["pending","blocked","decoder","ackworker","stoprace","externalclose"].includes(mode))
assert.equal(receipt?.state,mode==="pending"?"pending":"blocked");
if(mode==="transport")assert.equal(receipt?.state,"published");
assert(!JSON.stringify(f.out).includes('"payload"'));
const closed=f.out.find(v=>v.kind==="resident_closed");
assert.equal(closed.outcome,"resident_failed");
assert.equal(closed.transportReason,mode==="externalclose"?"unit_external_close":"resident_failed");
assert.deepEqual(closed.supplemental,[]);
const a=(await f.cli(["status","job-A"])).assignment;
assert.equal(a.no_resend,true);
if(mode==="transport"){
// Exception outside the runner's catch after completion: receipt preserved.
assert.equal(a.status,"complete");
assert.deepEqual(closed.reconciliation,{requestId:"job-A",receipt:"complete",transition:null});
}else{
// Every unresolved post-arm receipt is reconciled durably to indeterminate,
// including the pending return with submission_count still zero.
assert.equal(a.status,"indeterminate");
assert.equal(a.submission_count,0);
assert.equal(a.submission_may_have_occurred,true);
assert.equal(closed.reconciliation.requestId,"job-A");
assert.equal(closed.reconciliation.receipt,"indeterminate");
// The runner already reconciled its own catch; the pending return and the
// guard failures (whose guarded helper could not run) rely on the outer path.
const outer=["pending","owner","turn"].includes(mode);
assert.equal(closed.reconciliation.transition,outer?"indeterminate":null);
if(outer)assert.equal(closed.reconciliation.from,"armed");
}
const repeated=await f.repeat("job-A");
if(mode==="transport")assert.equal(repeated.state,"published");
else assert.equal(repeated.wake.status,"collect_only");
await assert.rejects(f.serve());
await missing(d+"/ready-2.json");await missing(d+"/wake.sock");
// The first transport close reason is immutable and reported as observed.
assert.equal(JSON.parse(await fs.readFile(d+"/transport-audit.json","utf8")).reason,closed.transportReason);
const failure=JSON.parse(await fs.readFile(d+"/resident-failure.json","utf8"));
assert.equal(failure.requestId,"job-A");
assert.equal(failure.sessionId,f.g.parkedResident.socket.config.sessionId);
assert.equal(failure.heldDelivery.requestId,"job-A");
assert.equal(failure.stopRequested,false);
assert.equal(typeof failure.error.name,"string");
assert.equal(typeof failure.error.message,"string");
assert.equal(typeof failure.error.stack,"string");
if(mode==="transport"){
assert.equal(failure.error.message,"Invalid tool result");
assert.equal(failure.error.detail.content.length,2);
}
if(mode==="stoprace")await fs.lstat(d+"/resident-stop.json");
else await missing(d+"/resident-stop.json");
// Canonical occupancy refuses first; a resident_failed audit alone never
// authorizes replacement either.
await assert.rejects(f.reopen(),/Authority occupied|not a matching closed/);
await missing(d+"/replacement-open.once.json");
assert.deepEqual(f.s.sends,["owner","turn"].includes(mode)?[]:["job-A"]);
});

// official_success_variants: the retained incident acknowledgment completes
// exactly one request through the resident, like the empty variant above.
test("resident accepts the incident send acknowledgment shape",async t=>{
const f=await fixture(t,"ack"),d=await f.open(),serving=f.serve();
await f.idle(1);await f.start(1,"job-A");await f.idle(2);
const collected=await f.cli(["queue","collect","job-A"]);
assert.equal(collected.state,"published");
const a=(await f.cli(["status","job-A"])).assignment;
assert.equal(a.status,"complete");assert.equal(a.submission_count,1);
await f.stop();await serving;
assert.deepEqual(f.s.sends,["job-A"]);
assert.equal(JSON.parse(await fs.readFile(d+"/transport-audit.json","utf8")).reason,"resident_stopped");
await missing(d+"/resident-failure.json");
assert.deepEqual(f.out.find(v=>v.kind==="resident_closed"),
{kind:"resident_closed",outcome:"resident_stopped",transportReason:"resident_stopped",reconciliation:null,supplemental:[]});
});

// A serve claim that executed but lost its acknowledgment still belongs to
// this invocation: it fails closed instead of leaving a consumed open owner,
// even when the turn changed and the old-turn gate would refuse.
for(const mode of ["claimlost","claimlostturn"])
test("lost serve-claim acknowledgment recovers ownership and fails closed: "+mode,async t=>{
const f=await fixture(t,mode),d=await f.open();
await assert.rejects(f.serve(),/Invalid tool result/);
assert.equal(f.s.waits,0);
assert.equal(f.g.parkedResident.used,true);
await missing(d+"/wake.sock");
assert.equal(JSON.parse(await fs.readFile(d+"/transport-audit.json","utf8")).reason,"resident_failed");
const failure=JSON.parse(await fs.readFile(d+"/resident-failure.json","utf8"));
assert.equal(failure.error.message,"Invalid tool result");
assert.equal(failure.error.detail.content.length,2);
assert.equal(failure.requestId,null);
const closed=f.out.find(v=>v.kind==="resident_closed");
assert.equal(closed.outcome,"resident_failed");assert.equal(closed.reconciliation,null);
await assert.rejects(f.serve(),mode==="claimlost"?/Serve consumed/:/turn changed/);
});

// A duplicate serve whose own claim reply is lost holds no token: it is
// refused without closing, cleaning up or reporting on the running owner.
test("duplicate serve with a lost claim reply never closes the running owner",async t=>{
const f=await fixture(t,"dupelost"),d=await f.open(),serving=f.serve();
await f.idle(1);
await assert.rejects(f.serve(),/Invalid tool result/);
assert.equal(f.s.claims,2);
await missing(d+"/transport-audit.json");await missing(d+"/resident-failure.json");
assert(!f.out.some(v=>v.kind==="resident_closed"));
await f.start(1,"job-A");await f.idle(2);
await f.stop();await serving;
assert.deepEqual(f.s.sends,["job-A"]);
assert.equal(JSON.parse(await fs.readFile(d+"/transport-audit.json","utf8")).reason,"resident_stopped");
});

// The runner's own unresolved helper is captured before the transport reply
// is decoded, drained once by cleanup, retained in the failure file, and
// blocks reconciliation while it stays pending.
test("runner-owned pending helper is captured, drained once and blocks reconciliation",async t=>{
const f=await fixture(t,"runnerpending"),d=await f.open(),serving=f.serve();
await f.idle(1);
const [served]=await Promise.allSettled([serving,f.start(1,"job-A")]);
assert.equal(served.status,"rejected");
assert.equal(f.s.drains,2); // one runner poll, one cleanup drain
assert.deepEqual(f.s.sends,[]);
const closed=f.out.find(v=>v.kind==="resident_closed");
assert.equal(closed.outcome,"resident_failed");
assert.equal(closed.transportReason,"resident_failed");
assert.deepEqual(closed.reconciliation,{requestId:"job-A",skipped:"helper_pending"});
assert.deepEqual(closed.supplemental,[{step:"helper_drain",message:"Helper still pending"}]);
const failure=JSON.parse(await fs.readFile(d+"/resident-failure.json","utf8"));
assert.equal(failure.pendingHelperSession,f.s.stuckSession);
assert.equal(failure.requestId,"job-A");
});

// A clean stop whose socket close or audit confirmation fails is not a clean
// service outcome; the transport reason is reported only when confirmed.
for(const mode of ["closefail","auditlost"])
test("clean stop with an unconfirmed close fails the service: "+mode,async t=>{
const f=await fixture(t,mode),d=await f.open(),serving=f.serve();
await f.idle(1);await f.stop();
await assert.rejects(serving,/Resident cleanup incomplete/);
const closed=f.out.find(v=>v.kind==="resident_closed");
assert.equal(closed.outcome,"resident_failed");
assert.equal(closed.transportReason,null);
assert.equal(closed.reconciliation,null);
assert.deepEqual(closed.supplemental.map(v=>v.step),["socket_close"]);
// The cleanup failure itself is persisted as the residence's failure record.
const failure=JSON.parse(await fs.readFile(d+"/resident-failure.json","utf8"));
assert.equal(failure.sessionId,f.g.parkedResident.socket.config.sessionId);
assert.equal(failure.cleanupStep,"socket_close");
assert.equal(failure.stopRequested,true);
assert.equal(failure.requestId,null);assert.equal(failure.heldDelivery,null);
assert.equal(failure.error.message,"Invalid tool result");
if(mode==="closefail"){await fs.lstat(d+"/wake.sock");await missing(d+"/transport-audit.json");}
else assert.equal(JSON.parse(await fs.readFile(d+"/transport-audit.json","utf8")).reason,"resident_stopped");
// Replacement is refused: no audit exists (closefail) or the failure file
// contradicts the resident_stopped audit (auditlost). Evidence is preserved.
await assert.rejects(f.reopen(),mode==="closefail"?/transport-audit\.json/:/Unproven resident artifacts/);
await fs.lstat(d+"/resident-failure.json");
await missing(d+"/replacement-open.once.json");
});

// When the primary failure was already persisted, a later close failure is
// only supplemental: the record is never rewritten or duplicated.
test("close failure after a persisted primary failure keeps that record",async t=>{
const f=await fixture(t,"blockedclosefail"),d=await f.open(),serving=f.serve();
await f.idle(1);
const [served]=await Promise.allSettled([serving,f.start(1,"job-A")]);
assert.equal(served.status,"rejected");
assert.match(served.reason.message,/Resident delivery unresolved/);
const closed=f.out.find(v=>v.kind==="resident_closed");
assert.equal(closed.outcome,"resident_failed");assert.equal(closed.transportReason,null);
assert.deepEqual(closed.supplemental.map(v=>v.step),["socket_close"]);
const failure=JSON.parse(await fs.readFile(d+"/resident-failure.json","utf8"));
assert.equal(failure.cleanupStep,undefined);
assert.match(failure.error.message,/Resident delivery unresolved/);
assert.equal(failure.requestId,"job-A");assert.equal(failure.heldDelivery.requestId,"job-A");
assert.equal((await f.cli(["status","job-A"])).assignment.status,"indeterminate");
assert.deepEqual(f.s.sends,["job-A"]);
});

// A helper whose host continuation is lost is joined once, reported, and the
// primary failure survives the drain failure; no canonical write races it.
test("lost helper host preserves the primary error and skips racing reconciliation",async t=>{
const f=await fixture(t,"helperlost"),d=await f.open(),serving=f.serve();
await f.idle(1);
const [served]=await Promise.allSettled([serving,f.start(1,"job-A")]);
assert.equal(served.status,"rejected");
assert.equal(served.reason.message,"host lost");
assert.equal(f.s.drains,2);
assert.deepEqual(f.s.sends,[]);
const closed=f.out.find(v=>v.kind==="resident_closed");
assert.equal(closed.outcome,"resident_failed");
assert.equal(closed.transportReason,"resident_failed");
assert.deepEqual(closed.supplemental,[{step:"helper_drain",message:"host lost again"}]);
assert.equal(closed.reconciliation,null);
const failure=JSON.parse(await fs.readFile(d+"/resident-failure.json","utf8"));
assert.equal(failure.pendingHelperSession,f.s.lostSession);
assert.equal(failure.error.message,"host lost");
await missing(d+"/wake.sock");
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
