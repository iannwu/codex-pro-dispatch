import test from "node:test";
import assert from "node:assert/strict";
import * as fs from "node:fs";
import {tmpdir} from "node:os";
import {execFile,spawnSync} from "node:child_process";
import {createHash} from "node:crypto";
import {fileURLToPath} from "node:url";
import {watchFile} from "../skills/codex-pro-dispatch/scripts/parked-activation.mjs";
const root=fileURLToPath(new URL("../",import.meta.url)),eq=assert.deepEqual;
const author=root+"skills/codex-pro-dispatch/scripts/parked-activation.mjs";
function fixture(t,confirm="--confirm-pro"){
const d=fs.realpathSync(fs.mkdtempSync(tmpdir()+"/activation-unit-"));
t.after(()=>fs.rmSync(d,{recursive:true}));
const env={...process.env,CODEX_PRO_DISPATCH_HOME:d+"/authority"};
function run(file,args,input=""){
const r=spawnSync(file,args,{env,input,encoding:"utf8",timeout:30000,maxBuffer:8388608});
if(r.error) throw r.error;
return [r.status,JSON.parse(r.status?r.stderr:r.stdout)];
}
const cli=(a,input)=>run("python3",[root+"bin/pro-dispatch",...a],input);
const invoke=a=>run(process.execPath,[author,...a]);
eq(cli(["worker","set","--conversation-id","unit-pro",confirm,"--native-controls-confirmed"])[0],0);
return {d,cli,invoke};
}
const sha256=path=>createHash("sha256").update(fs.readFileSync(path)).digest("hex");
test("an early watcher event gets one bounded deferred recheck",async()=>{
let notify,available=false,closed=false,reads=0;
const watcher=(directory,listener)=>{
assert.equal(directory,"fixture");notify=listener;
return {on(){return this;},close(){closed=true;}};
};
const waiting=watchFile("fixture",1000,"timeout",async()=>{
reads++;
if(!available){const error=Error("missing");error.code="ENOENT";throw error;}
return "ready";
},watcher);
await new Promise(resolve=>setImmediate(resolve));
notify("rename","ready.tmp");
available=true;
assert.equal(await waiting.promise,"ready");
assert.equal(reads,3);
assert.equal(closed,true);
});
test("packet pins match the exact bytes of the pinned scripts",t=>{
const f=fixture(t),[code,p]=f.invoke(["packet","broker","parent","unit-pro"]);
eq(code,0);
eq(Object.keys(p.pins).sort(),["parked-client.mjs","parked-runner.js","parked-socket.mjs","resident-supervision.mjs"]);
for(const [name,hash] of Object.entries(p.pins))
eq(hash,sha256(root+"skills/codex-pro-dispatch/scripts/"+name));
});
for(const [marker,flag] of [["user-confirmed-worker","--confirm-worker"],["user-confirmed-pro","--confirm-pro"]])
test("packets accept the "+marker+" worker marker",t=>{
const f=fixture(t,flag);
eq(f.cli(["worker","show"])[1].worker.model_confirmation,marker);
eq(f.invoke(["packet","broker","parent","unit-pro"])[0],0);
const root=f.d+"/client";fs.mkdirSync(root,{mode:448});
eq(f.invoke(["resident-packet","parent","parent","unit-pro",root])[0],0);
eq(f.cli(["status"])[1].active_assignment,null);
});
const tree=dir=>Object.fromEntries(fs.readdirSync(dir,{recursive:true}).filter(n=>fs.statSync(dir+"/"+n).isFile())
.map(n=>[n,fs.readFileSync(dir+"/"+n,"hex")]));
for(const marker of ["user-confirmed-other","",null,["user-confirmed-worker"]])
test("packets refuse the "+JSON.stringify(marker)+" worker marker",t=>{
const f=fixture(t),file=f.d+"/authority/config/worker.json";
const record=JSON.parse(fs.readFileSync(file,"utf8"));
fs.writeFileSync(file,JSON.stringify({...record,model_confirmation:marker}),{mode:384});
const root=f.d+"/client";fs.mkdirSync(root,{mode:448});
const before=tree(f.d+"/authority");
for(const args of [["packet","broker","parent","unit-pro"],["resident-packet","parent","parent","unit-pro",root]]){
const [code,r]=f.invoke(args);
assert.notEqual(code,0);assert.match(r.error,/status validation failed/);
}
eq(tree(f.d+"/authority"),before);
eq(f.cli(["worker","show"])[0],2);
});
test("packet is complete; no native activation",t=>{
const f=fixture(t),[code,p]=f.invoke(["packet","broker","parent","unit-pro"]);
eq(code,0);eq(p.authorization,"required_separately");
eq(p.trusted.stateDir,f.d+"/authority/state");
const AF=Object.getPrototypeOf(async()=>{}).constructor;
for(const [name,body] of Object.entries(p.calls)){
assert.doesNotThrow(()=>new AF("tools","text",body));
const ms=name==="dispatch"?1000:60000;
assert(body.startsWith('// @exec: {"yield_time_ms":'+ms+'}'));
}
eq(p.trusted.idleMs,45000);
eq(p.trusted.activeJobMs,3600000);
eq(p.trusted.replyMs,3900000);
eq(p.trusted.leaseMs,7200000);
assert(p.calls.dispatch.includes("Native broker task/turn changed"));
eq(f.cli(["status"])[1].active_assignment,null);
eq(f.cli(["queue","status"])[1].requests,[]);
});
function activatePool(f, workers){
const home=f.d+"/authority";
const evidence={
kind:"legacy_quiescence",config_dir:home+"/config",state_dir:home+"/state",
implementation:"isolated fixture",observations:"no live owner or listener in fixture",
authorization:"test-only explicit maintenance evidence",physical_quiescence:true
};
const evidencePath=f.d+"/pool-evidence.json";
fs.writeFileSync(evidencePath,JSON.stringify(evidence),{mode:384});
const workersPath=f.d+"/workers.json";
fs.writeFileSync(workersPath,JSON.stringify(workers),{mode:384});
eq(f.cli(["worker-pool","activate","--workers-file",workersPath,
"--expected-legacy-sha256",sha256(home+"/config/worker.json"),
"--evidence-file",evidencePath,"--evidence-sha256",sha256(evidencePath),
"--native-controls-confirmed"])[0],0);
return {evidencePath,workersPath};
}
for(const pool of [false,true])test(`resident ${pool?"pool":"scalar"} packet carries active-turn supervision into the yielded cell`,async t=>{
const f=fixture(t),root=f.d+"/client";fs.mkdirSync(root,{mode:448});
if(pool)activatePool(f,[{slot:"slot-a",conversation_id:"unit-pro",label:"A",
model_confirmation:"user-confirmed-worker",configured_at:"fixture"}]);
const [code,p]=f.invoke([pool?"resident-pool-packet":"resident-packet","parent","parent",
pool?JSON.stringify(["unit-pro"]):"unit-pro",root]);
eq(code,0);
assert.equal(p.lifecycle.mode,"active_owner_turn");
assert.equal(p.lifecycle.detachedSupported,false);
assert.equal(p.lifecycle.onYield.tool,"functions.wait");
assert.equal(p.lifecycle.onYield.yield_time_ms,60000);
assert.match(p.lifecycle.onYield.cell_id,/returned/);
assert.match(p.lifecycle.instruction,/Do not send a final response/);
// Execute the generated body through its first actual native call. The outer
// host must receive the supervision contract even when startup later fails.
const out=[],sentinel=Error("fixture stops before native ownership");
const AF=Object.getPrototypeOf(async()=>{}).constructor;
await assert.rejects(new AF("tools","text",p.calls.serve)({
mcp__node_repl__js:async()=>{throw sentinel;}
},value=>out.push(value)),e=>e===sentinel);
assert.equal(out[0].kind,"resident_supervision_required");
assert.equal(out[0].admissionObserved,false);
eq(out[0].lifecycle,p.lifecycle);
});
test("one-worker pool uses the pool resident packet",t=>{
const f=fixture(t,"--confirm-worker");
const root=f.d+"/client";fs.mkdirSync(root,{mode:448});
activatePool(f,[{slot:"slot-a",conversation_id:"unit-pro",label:"A",
model_confirmation:"user-confirmed-worker",configured_at:"fixture"}]);
const [code,p]=f.invoke(["resident-pool-packet","parent","parent",
JSON.stringify(["unit-pro"]),root]);
eq(code,0);
eq(p.trusted.maxConcurrentRequests,1);
eq(p.trusted.workers.map(w=>w.conversation_id),["unit-pro"]);
assert.match(p.calls.serve,/preparedRecovery/);
assert.match(p.calls.serve,/collectOnly:false/);
assert.match(p.calls.serve,/status==="pending"\|\|status==="not_submitted"/);
assert.match(p.calls.serve,/pendingCollect/);
assert.match(p.calls.serve,/status==="pending"/);
assert.match(p.calls.serve,/residentInvocation\?\.generation/);
assert.match(p.calls.serve,/job.held/);
assert.match(p.calls.serve,/waitResidentStop/);
assert.match(p.calls.serve,/Admitted pool work remained queued/);
assert.match(p.calls.serve,/begun\.worker_slot/);
assert.match(p.calls.serve,/slot:begun\.worker_slot/);
assert.match(p.calls.serve,/queue record may still be absent/);
assert.match(p.calls.serve,/worker:result\.worker_conversation_id,slot:result\.worker_slot/);
assert.doesNotMatch(p.calls.serve,/worker:result\.worker_slot/);
assert.doesNotMatch(p.calls.serve,/not_submitted"\]\.includes/);
const [scalarCode,scalar]=f.invoke(["resident-packet","parent","parent","unit-pro",root]);
assert.notEqual(scalarCode,0);
assert.match(scalar.error,/pool resident packet/);
const [twoCode,two]=f.invoke(["resident-pool-packet","parent","parent",
JSON.stringify(["unit-pro","worker-b"]),root]);
assert.notEqual(twoCode,0);
assert.match(two.error,/worker pool mismatch|One or two trusted worker/i);
});
test("two-worker pool packet keeps capacity two",t=>{
const f=fixture(t,"--confirm-worker");
const root=f.d+"/client";fs.mkdirSync(root,{mode:448});
activatePool(f,[
{slot:"slot-a",conversation_id:"unit-pro",label:"A",
model_confirmation:"user-confirmed-worker",configured_at:"fixture"},
{slot:"slot-b",conversation_id:"unit-other",label:"B",
model_confirmation:"user-confirmed-pro",configured_at:"fixture"}
]);
const [code,p]=f.invoke(["resident-pool-packet","parent","parent",
JSON.stringify(["unit-pro","unit-other"]),root]);
eq(code,0);
eq(p.trusted.maxConcurrentRequests,2);
eq(p.trusted.workers.map(w=>w.conversation_id),["unit-pro","unit-other"]);
});
test("pool rendezvous carries the bound hash through real resident admission",async t=>{
const f=fixture(t,"--confirm-worker"),home=f.d+"/authority";
const {evidencePath}=activatePool(f,[{slot:"slot-a",conversation_id:"unit-pro",label:"A",
model_confirmation:"user-confirmed-worker",configured_at:"fixture"}]);
const enrolled=f.cli(["resident","enroll",JSON.stringify({generation:0,
owner:"fixture-enrollment",parent:"parent",evidence_file:evidencePath,
evidence_sha256:sha256(evidencePath)})])[1].owner;
const started=f.cli(["resident","start",JSON.stringify({generation:enrolled.generation,
owner:"fixture-owner",parent:"parent",worker_pool_sha256:enrolled.worker_pool_sha256})])[1].owner;
const directory=fs.realpathSync(fs.mkdtempSync(f.d+"/session-"));
const sessionId="a".repeat(32),descriptor={
helper:root+"skills/codex-pro-dispatch/scripts/pro-dispatch",
configDir:home+"/config",stateDir:home+"/state",parent:"parent",
workers:[{slot:"slot-a",conversation_id:"unit-pro"}],
worker_pool_sha256:started.worker_pool_sha256,workerPoolSha256:started.worker_pool_sha256,
resident:true,maxConcurrentRequests:1,
leaseMs:null,idleMs:45000,replyMs:30000,sessionId,token:"b".repeat(48),expiresAt:null
};
const raw=JSON.stringify(descriptor);
fs.writeFileSync(directory+"/session.json",raw,{mode:384});
const session={directory,session_id:sessionId,
descriptor_sha256:createHash("sha256").update(raw).digest("hex")};
eq(f.cli(["resident","bind-session",JSON.stringify({generation:started.generation,
owner:started.owner,parent:started.parent,worker_pool_sha256:started.worker_pool_sha256,
session})])[0],0);
fs.mkdirSync(directory+"/waiting-1."+sessionId,{mode:448});
const prompt=f.d+"/pool-prompt.txt";
fs.writeFileSync(prompt,"Pool admission regression.",{mode:384});
let completed;
const child=new Promise(resolve=>execFile(process.execPath,[author,"rendezvous",directory,
"1","pool-request",prompt,"pool-client"],
{env:{...process.env,CODEX_PRO_DISPATCH_HOME:home},timeout:30000,maxBuffer:8388608},
(error,stdout,stderr)=>resolve({error,stdout,stderr}))).then(result=>(completed=result,result));
const command=directory+"/command-1.json";
for(let i=0;i<500&&!fs.existsSync(command);i++)await new Promise(resolve=>setTimeout(resolve,10));
assert.equal(fs.existsSync(command),true,
"real pool admission must publish the command: "+(completed?.stderr||"still running"));
const commandRaw=fs.readFileSync(command);
fs.writeFileSync(directory+"/command-observed-1.json",commandRaw,{mode:384});
fs.writeFileSync(directory+"/ready-1.json",commandRaw,{mode:384});
const result=await child;
assert(result.error,"the fixture intentionally has no wake socket");
assert.doesNotMatch(result.stderr,/Resident pool owner replaced/);
eq(f.cli(["resident","inspect"])[1].owner.worker_pool_sha256,started.worker_pool_sha256);
});
for(const busy of [true,false]) test(busy?"busy preserved":"wrong worker refused",t=>{
const f=fixture(t);
if(busy) eq(f.cli(["prepare","--assignment-id","unit-active","--parent-task-id",
"other-parent","--native-controls-confirmed"],"fixture")[0],0);
const before=f.cli(["status"])[1];
const [code,r]=f.invoke(["packet","broker","parent",busy?"unit-pro":"wrong"]);
assert.notEqual(code,0);assert.match(r.error,busy?/occupied/:/Production worker/);
eq(f.cli(["status"])[1],before);
});
test("readiness record, no native activation",t=>{
const f=fixture(t),d=f.d+"/session",sessionId="a".repeat(32);
fs.mkdirSync(d,{mode:448});
for(const [name,data] of [["session",{sessionId,leaseMs:20000,idleMs:5000,
replyMs:10000,expiresAt:Date.now()+20000}],
["ready-1",{sessionId,ordinal:1}]])
fs.writeFileSync(d+"/"+name+".json",JSON.stringify(data),{mode:384});
const [code,r]=f.invoke(["ready",d,"1"]);
eq([code,r.ready,r.ordinal],[0,true,1]);
assert.match(r.meaning,/not an admission guarantee/);
eq(f.cli(["status"])[1].active_assignment,null);
});
