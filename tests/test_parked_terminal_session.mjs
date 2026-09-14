import test from "node:test";
import assert from "node:assert/strict";
import * as fs from "node:fs/promises";
import {tmpdir} from "node:os";
import {execFile} from "node:child_process";
import {createHash} from "node:crypto";
import {openSession} from "../skills/codex-pro-dispatch/scripts/parked-socket.mjs";

const scripts=new URL("../skills/codex-pro-dispatch/scripts/",import.meta.url);
const AF=Object.getPrototypeOf(async function(){}).constructor,J=JSON.stringify;
const sha=b=>createHash("sha256").update(b).digest("hex");
const rid="terminal-unit",parent="unit-parent",worker="current-pro",historical="old-pro";
const laterId="later-unit";
async function absent(p){await assert.rejects(fs.lstat(p),e=>e.code==="ENOENT");}

// Synthetic evidence and read-only CLI double; real activation/socket code.
async function fixture(t,lease=false,later=false){
const base=process.platform==="darwin"?"/private/tmp":tmpdir();
const d=await fs.realpath(await fs.mkdtemp(base+"/pro-terminal-unit-"));
const home=d+"/authority",oldDir=d+"/old",current=d+"/current";
const oldHelper=d+"/historical/pro-dispatch",marker=oldDir+"/replacement-open.once.json";
let old,runtime;
await fs.writeFile(d+"/calls","");
t.after(async()=>{
try{
await old?.close("unit_cleanup");
if(runtime?.parkedSocket&&runtime.parkedSocket!==old)
await runtime.parkedSocket.close("unit_cleanup");
await absent(d+"/old-executed");
const calls=(await fs.readFile(d+"/calls","utf8")).trim().split("\n").filter(Boolean);
assert(calls.every(a=>["status --current","queue status","queue status "+rid,
"status "+rid,"status "+laterId].includes(a)),"only read-only helper commands");
}finally{await fs.rm(d,{recursive:true,force:true});}
});
await fs.cp(scripts,current,{recursive:true});
for(const p of [home,home+"/config",home+"/state",home+"/state/assignments",oldDir,d+"/historical"])
await fs.mkdir(p,{mode:448});
await fs.writeFile(oldHelper,`#!/usr/bin/env python3
open(${J(d+"/old-executed")},"w").close()
raise RuntimeError("historical helper executed")
`,{mode:448});
await fs.writeFile(current+"/pro-dispatch",`import json,sys
from pathlib import Path
p=Path(__file__).parent.parent
key=" ".join(sys.argv[1:])
with (p/"calls").open("a") as f: f.write(key+"\\n")
v=json.loads((p/"canonical.json").read_text())[key]
print(json.dumps(v),file=sys.stdout if v.get("ok") else sys.stderr)
sys.exit(0 if v.get("ok") else 1)
`);
old=await openSession(oldDir,{
helper:oldHelper,configDir:home+"/config",stateDir:home+"/state",
worker:historical,parent,leaseMs:60000,idleMs:30,replyMs:10000
});
assert.equal(await old.receive(),null);
await old.close();
if(lease){
old.config.expiresAt=Date.now()-1000;
await fs.writeFile(oldDir+"/session.json",J(old.config));
}
const binding=Object.freeze({broker:parent,turn:"old-turn"});
runtime={parkedSocket:old,parkedBinding:binding,parkedDelivery:null};
const sessionId=old.config.sessionId,callId="c".repeat(32),nonce="d".repeat(32);
const raw="unit prompt\n",sent=sha("wrapped prompt"),fingerprint=sha("immutable identity");
const at=lease?old.config.expiresAt-5000:Date.now(),command={sessionId,ordinal:1,requestId:rid,
clientSessionId:"unit-client",nonce,deadlineAt:old.config.expiresAt,
promptSha256:sha(raw),pid:1,ppid:1};
const evidence={
"transport-audit.json":J({sessionId,reason:lease?"lease_expired":"retired_after_job",events:[
{name:"accepted",at,callId,operation:"run",requestId:rid},
{name:"finished",at:at+1,callId,disposition:lease?"published":"blocked",requestId:rid}]}),
"command-1.json":J(command),"command-observed-1.json":J(command),
"ready-1.json":J({at,ordinal:1,sessionId}),"command-1/prompt.txt":raw
};
await fs.mkdir(oldDir+"/command-1",{mode:448});
if(later){
await fs.mkdir(oldDir+"/command-2",{mode:448});
evidence["command-2.json"]=J({...command,ordinal:2,requestId:laterId,
clientSessionId:"later-client",nonce:"e".repeat(32),deadlineAt:old.config.expiresAt-1000});
evidence["command-2/prompt.txt"]=raw;
}
for(const [name,value] of Object.entries(evidence))
await fs.writeFile(oldDir+"/"+name,value,{mode:384});
evidence["session.json"]=await fs.readFile(oldDir+"/session.json");
const row={ok:true,request_id:rid,client_session_id:"unit-client",fingerprint,
state:"acknowledged",dispatch_status:"complete",sent_verified:true,
send_authorized:false,send_may_have_occurred:true,
parent_task_id:parent,worker_conversation_id:historical};
const receipt={assignment_id:rid,parent_task_id:parent,
worker_conversation_id:historical,status:"complete",submission_count:1,
submission_observed:true,outbound_prompt_verified:true,sent_prompt_sha256:sent,
wrapped_prompt_sha256:sent,result_marker_validated:true,
verification_level:"bounded_native_summary",native_collection:{worker_id:historical},
generation_finality_verified:false,source_bytes_verified:false};
const data={
["status "+laterId]:{ok:false,error_type:"ConfigurationError",details:{},
error:"Missing file: "+home+"/state/assignments/"+laterId+".json"},
["queue status "+rid]:row,["status "+rid]:{ok:true,assignment:receipt},
"queue status":{ok:true,requests:[row]},
"status --current":{ok:true,active_assignment:null,active_cooldown:null,
worker:{conversation_id:worker,model_confirmation:"user-confirmed-pro"},
paths:{config_dir:home+"/config",state_dir:home+"/state"}}
};
const save=()=>fs.writeFile(d+"/canonical.json",J(data));
await save();
const args=[parent,parent,worker,oldDir,oldHelper,historical,sessionId,rid,callId,
"unit-client",fingerprint,nonce,sha(raw),sent,sha(evidence["session.json"]),
sha(evidence["transport-audit.json"])];
if(later)args.push(laterId,sha(evidence["command-2.json"]),sha(raw));
const generate=()=>new Promise((resolve,reject)=>{
const child=execFile(process.execPath,[current+"/parked-activation.mjs",
"closed-terminal-packet",...args],{
env:{...process.env,CODEX_PRO_DISPATCH_HOME:home},timeout:15000,maxBuffer:8388608
},(error,out,err)=>{
try{
const v=JSON.parse(error?err:out);
if(error)throw Error(v.error||err);
resolve(v);
}catch(e){reject(e);}
});
child.stdin?.end();
});
async function run(p){
const output=[],tools={async mcp__node_repl__js(a){
assert.equal(a.timeout_ms,60000);
const lines=[];
await new AF("nodeRepl","globalThis","console",a.code)({
tmpDir:d,requestMeta:{threadId:parent,"x-codex-turn-metadata":{turn_id:"new-turn"}}
},runtime,{log:v=>lines.push(String(v))});
return {isError:false,content:[{type:"text",text:lines.join("\n")}]};
}};
await new AF("tools","text",p.calls.open)(tools,v=>output.push(v));
assert.equal(output.length,1);
return JSON.parse(output[0].content[0].text);
}
const freshDirs=async()=>(await fs.readdir(d)).filter(n=>n.startsWith("pro-session-"));
async function untouched(){
assert.equal(runtime.parkedSocket,old);assert.equal(runtime.parkedBinding,binding);
await absent(marker);assert.deepEqual(await freshDirs(),[]);
}
return {home,args,oldDir,oldHelper,marker,old,binding,runtime,data,row,receipt,
evidence,save,generate,run,freshDirs,untouched};
}

for(const [lease,later] of [[false,false],[true,false],[true,true]])
test("terminal replacement syncs and opens once: "+lease+"/"+later,async t=>{
const f=await fixture(t,lease,later),packets=[await f.generate(),await f.generate()];
assert.notEqual(packets[0].trusted.helper,f.oldHelper);
assert.equal(f.old.config.worker,historical);
const handle=await fs.open(f.oldDir,"r"),dirStat=await handle.stat();
const proto=Object.getPrototypeOf(handle),original=proto.sync,synced=[];
await handle.close();
t.mock.method(proto,"sync",async function(){
const s=await this.stat(),m=await fs.stat(f.marker);
const same=v=>v.dev===s.dev&&v.ino===s.ino;
const kind=same(dirStat)?"directory":same(m)?"marker":null;
await original.call(this);
if(kind){assert.deepEqual(await f.freshDirs(),[]);synced.push(kind);}
});
const results=await Promise.allSettled(packets.map(p=>f.run(p)));
assert.equal(results.filter(r=>r.status==="fulfilled").length,1);
const i=results.findIndex(r=>r.status==="fulfilled"),p=packets[i],r=results[i].value;
assert.equal(r.replacedSessionId,f.old.config.sessionId);
assert.notEqual(r.sessionId,f.old.config.sessionId);
assert.deepEqual(f.runtime.parkedBinding,{broker:parent,turn:"new-turn"});
assert.equal(f.runtime.parkedSocket.config.worker,worker);
assert.equal(f.runtime.parkedDelivery,null);
for(const [name,value] of Object.entries(f.evidence))
assert.deepEqual(await fs.readFile(f.oldDir+"/"+name),Buffer.from(value));
await absent(f.oldDir+"/wake.sock");await absent(r.directory+"/ready-1.json");
const m=JSON.parse(await fs.readFile(f.marker,"utf8"));
if(later)assert.deepEqual(m.terminal.unobserved,{requestId:laterId,
commandSha256:sha(f.evidence["command-2.json"]),
promptSha256:sha(f.evidence["command-2/prompt.txt"])});
for(const k of ["terminal","evidenceSha256","terminalReceiptSha256","auditSha256"])
assert.deepEqual(m[k],p.previous[k]);
assert.equal(m.previousSessionId,f.old.config.sessionId);
assert.deepEqual(m.previousBinding,f.binding);assert.equal(m.attempt,p.openAttempt);
assert.equal((await fs.stat(f.marker)).mode&511,384);
assert.deepEqual(synced,["marker","directory"]);
for(const packet of packets)await assert.rejects(f.run(packet));
await assert.rejects(f.generate());
assert.equal((await f.freshDirs()).length,1);
});

test("unobserved lease proof rejects new evidence or canonical authority",async t=>{
for(const kind of ["queued","published","acknowledged","claimed","assignment","error",
"receipt-file","command-2.json","command-2/prompt.txt","ready-2.json",
"command-observed-2.json","extra"])
await t.test(kind,async t=>{
const f=await fixture(t,true,true),p=await f.generate();
if(["queued","published","acknowledged","claimed"].includes(kind))
f.data["queue status"].requests.push({request_id:laterId,state:kind});
else if(kind==="assignment")f.data["status "+laterId]={ok:true,assignment:{status:"complete"}};
else if(kind==="error")f.data["status "+laterId].error="Permission denied";
else if(kind==="receipt-file")
await fs.writeFile(f.home+"/state/assignments/"+laterId+".json","{}",{mode:384});
else await fs.appendFile(f.oldDir+"/"+kind,"\n",{mode:384});
await f.save();
await assert.rejects(f.generate());
await assert.rejects(f.run(p));
await f.untouched();
});
});

test("operator pins cannot authorize another ordinal, identity or operation",async t=>{
for(const change of [
v=>v.ordinal=3,v=>v.sessionId="0".repeat(32),v=>v.requestId=rid,
v=>v.clientSessionId="../bad",v=>v.nonce="d".repeat(32),
v=>v.deadlineAt=Date.now()+60000,v=>v.operation="resume"
])await t.test("strict command shape",async t=>{
const f=await fixture(t,true,true);
const file=f.oldDir+"/command-2.json",v=JSON.parse(await fs.readFile(file,"utf8"));
change(v);await fs.writeFile(file,J(v));f.args[17]=sha(J(v));
await assert.rejects(f.generate());await f.untouched();
});
const f=await fixture(t,false,true);
await assert.rejects(f.generate());await f.untouched();
});

test("terminal eligibility is checked again inside open",async t=>{
for(const [name,edit,eligible=false] of [
["queued",f=>f.row.state="queued"],
["published",f=>f.row.state="published"],
["missing queue",f=>f.data["queue status"].requests=[]],
["duplicate queue",f=>f.data["queue status"].requests.push(f.row)],
["missing assignment",f=>delete f.data["status "+rid].assignment],
["submission count",f=>f.receipt.submission_count=2],
["parent",f=>f.receipt.parent_task_id="other"],
["current worker",f=>f.data["status --current"].worker.conversation_id="other"],
["receipt changed",f=>f.receipt.source_bytes_verified=true,true]
])await t.test(name,async t=>{
const f=await fixture(t),p=await f.generate();
edit(f);await f.save();
if(eligible)await f.generate();else await assert.rejects(f.generate());
await assert.rejects(f.run(p),/Canonical terminal|Ambiguous|authority mismatch|Terminal receipt changed/);await f.untouched();
});
});

test("changed evidence or retained delivery rejects open",async t=>{
for(const name of ["session.json","transport-audit.json","command-1.json",
"command-observed-1.json","ready-1.json","command-1/prompt.txt","delivery"])
await t.test(name,async t=>{
const f=await fixture(t),p=await f.generate();
if(name==="delivery")f.runtime.parkedDelivery={requestId:"held"};
else await fs.appendFile(f.oldDir+"/"+name,"\n");
await assert.rejects(f.run(p),name==="delivery"?/not recoverable/:/evidence changed/);
await f.untouched();
});
});
