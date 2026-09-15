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
const activate=args=>json(process.execPath,[author,...args]);
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
const result=await pending;
if(a.cmd.includes("'arm'")){
if(mode==="turn")meta["x-codex-turn-metadata"].turn_id="changed";
if(mode==="owner")g.parkedBinding=Object.freeze({...g.parkedBinding});
}
return result;
},
async write_stdin(a){
assert.equal(a.chars,"");assert(sessions.has(a.session_id));
const r=await sessions.get(a.session_id);sessions.delete(a.session_id);return r;
},
async mcp__codex_app__read_thread(a){
assert.equal(a.threadId,W);s.reads++;
if(mode==="blocked"&&last)return {isError:true};
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
assert(id);last={id,prompt:a.prompt};s.sends.push(id);return mcp({});
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
const reopen=async()=>{
p=await activate(["closed-resident-packet",P,P,W,g.parkedResident.directory,d]);
return open();
};
return {g,meta,s,out,cli,open,serve,start,stop,repeat,reopen,activate,d,
idle:n=>until(()=>s.waits>=n)};
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
await assert.rejects(f.serve(),/Serve consumed/);
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
// Denial/cancellation uses the normal stop, not an alternate submit route.
await f.stop();await serving;
assert.deepEqual(f.s.sends,[]);
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

for(const mode of ["pending","blocked","owner","turn"])
test("resident stops and retains delivery: "+mode,async t=>{
const f=await fixture(t,mode),d=await f.open(),serving=f.serve();
await f.idle(1);
await Promise.allSettled([serving,f.start(1,"job-A")]);
assert.equal(f.s.waits,1);
assert.equal(f.g.parkedDelivery?.requestId,"job-A");
if(["pending","blocked"].includes(mode))
assert.equal(f.out.find(v=>v.kind==="runner_receipt")?.state,mode);
assert.equal((await f.cli(["status","job-A"])).assignment.no_resend,true);
assert.equal((await f.repeat("job-A")).wake.status,"collect_only");
await assert.rejects(f.serve());
await missing(d+"/ready-2.json");await missing(d+"/wake.sock");
assert.deepEqual(f.s.sends,["pending","blocked"].includes(mode)?["job-A"]:[]);
});
