import * as fs from "node:fs/promises";
import {watch,constants} from "node:fs";
import {dirname,join} from "node:path";
import {fileURLToPath,pathToFileURL} from "node:url";
import {execFile} from "node:child_process";
import {createHash,randomBytes} from "node:crypto";

// Model-facing receipt only. Full results remain in the client transport and
// canonical evidence. Never spread a result or tool envelope into this receipt.
export function runnerReceipt(result) {
const id=v=>typeof v==="string"&&/^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/.test(v)?v:null;
const state=result?.observation||result?.state;
return {kind:"runner_receipt",request_id:id(result?.request_id),
ok:result?.ok===true,
state:["published","acknowledged","pending","blocked","not_submitted"].includes(state)?state:"unknown",
no_resend:result?.no_resend===true,
diagnostic_review_required:result?.diagnostic_review_required===true,
pending_helper_session:Number.isInteger(result?.pending_helper_session)?result.pending_helper_session:null};
}

const dir=await fs.realpath(dirname(fileURLToPath(import.meta.url)));
// The native REPL does not expose process. Its reviewed module directory is
// owned by the same user as its private session files.
const ownerUid=typeof process==="undefined"?(await fs.stat(dir)).uid:process.getuid();
const helper=join(dir,"pro-dispatch"),J=JSON.stringify;
const pins={
  "parked-runner.js":"781cf78be08efa025efd86e07fa1d43f1e1639f73bd43b291ab15d1441368860",
"parked-socket.mjs":"65b791760427c44ddd5afdf4756e02d73b886cd15dfe3028ed6b0dbbb2135ffc",
"parked-client.mjs":"f77698abac3f59ba0da48a39dfe6a4d8e3583f26b8d16b5e58975be2ecb22d27"
};
const sources={};
for(const [name,hash] of Object.entries(pins)){
const path=join(dir,name),raw=await fs.readFile(path);
if(await fs.realpath(path)!==path||
createHash("sha256").update(raw).digest("hex")!==hash)
throw Error("Pinned script mismatch: "+name);
sources[name]=raw.toString("utf8");
}
function cli(args,timeout=30000,missingPath,input){
return new Promise((resolve,reject)=>{
const child=execFile("python3",[helper,...args],{
timeout,maxBuffer:8388608
},(error,out,err)=>{
try{
const v=JSON.parse(error?err:out);
if(missingPath!==undefined){
if(!Number.isInteger(error?.code)||error.code===0||error.killed||error.signal||
out.trim()||v?.ok!==false||v.error_type!=="ConfigurationError"||
v.error!=="Missing file: "+missingPath||JSON.stringify(v.details)!=="{}"||
Object.keys(v).sort().join(",")!=="details,error,error_type,ok")
throw Error("Canonical absence unproven");
}else if(error||v.ok!==true) throw Error(v.error||"Helper failed");
resolve(v);
}catch(e){reject(e);}
});
child.stdin.on("error",reject);
child.stdin.end(input);
});
}
function resumeExpectation(r){
if(!r||Object.keys(r).sort().join(",")!==
"callId,clientSessionId,fingerprint,nonce,promptSha256,requestId,sessionId"||
!["requestId","clientSessionId"].every(k=>validId(r[k]))||
!["sessionId","callId","nonce"].every(k=>
typeof r[k]==="string"&&/^[a-f0-9]{32}$/.test(r[k]))||
!["fingerprint","promptSha256"].every(k=>
typeof r[k]==="string"&&/^[a-f0-9]{64}$/.test(r[k])))
throw Error("Exact closed queued-request expectations required");
}

function queuedCheckArgs(r,worker){
return ["queue","resume-check",r.requestId,"--fingerprint",r.fingerprint,
"--worker-conversation-id",worker,"--client-session-id",r.clientSessionId,
"--raw-prompt-sha256",r.promptSha256];
}

function requireQueuedCheck(v,r,c){
if(v?.ok!==true||v.state!=="queued"||v.resume_eligible!==true||
v.assignment_absent!==true||v.send_authorized!==false||
v.request_id!==r.requestId||v.fingerprint!==r.fingerprint||
v.client_session_id!==r.clientSessionId||v.raw_prompt_sha256!==r.promptSha256||
v.worker_conversation_id!==c.worker||
v.worker_model_confirmation!=="user-confirmed-pro"||
v.paths?.config_dir!==c.configDir||v.paths?.state_dir!==c.stateDir)
throw Error("Canonical queued-resume proof does not match trusted session");
}

function requireClosedQueued(saved,audit,command,observed,ready,r,terminalLease=false){
const shape=(v,keys)=>v&&typeof v==="object"&&!Array.isArray(v)&&
Object.keys(v).sort().join(",")===keys;
if(saved.sessionId!==r.sessionId||saved.queuedResume!==undefined||
!shape(audit,"events,reason,sessionId")||
audit.sessionId!==r.sessionId||
audit.reason!==(terminalLease?"lease_expired":"retired_after_job")||
!Array.isArray(audit.events)||audit.events.length!==2)
throw Error("Not the expected closed queued-session audit");
const [a,b]=audit.events;
if(!shape(a,"at,callId,name,operation,requestId")||
!shape(b,"at,callId,disposition,name,requestId")||
a.name!=="accepted"||b.name!=="finished"||a.operation!=="run"||
b.disposition!==(terminalLease?"published":"blocked")||
a.callId!==r.callId||b.callId!==r.callId||
a.requestId!==r.requestId||b.requestId!==r.requestId||
!Number.isSafeInteger(a.at)||!Number.isSafeInteger(b.at))
throw Error("Audit is not the exact accepted/finished "+
(terminalLease?"published":"blocked")+" pair");
if(!shape(command,
"clientSessionId,deadlineAt,nonce,ordinal,pid,ppid,promptSha256,requestId,sessionId")||
JSON.stringify(command)!==JSON.stringify(observed)||
command.sessionId!==r.sessionId||command.ordinal!==1||
command.requestId!==r.requestId||command.clientSessionId!==r.clientSessionId||
command.nonce!==r.nonce||command.promptSha256!==r.promptSha256||
!Number.isSafeInteger(command.deadlineAt)||
!Number.isSafeInteger(saved.expiresAt)||command.deadlineAt>saved.expiresAt||
!Number.isSafeInteger(command.pid)||command.pid<1||
!Number.isSafeInteger(command.ppid)||command.ppid<1||
!shape(ready,"at,ordinal,sessionId")||ready.sessionId!==r.sessionId||
ready.ordinal!==1||!Number.isSafeInteger(ready.at))
throw Error("Closed-session command/readiness association differs");
if(terminalLease&&(Date.now()<saved.expiresAt||ready.at<1||
ready.at>=command.deadlineAt||a.at<ready.at||b.at<a.at||b.at>saved.expiresAt))
throw Error("Completed lease-session chronology differs");
}

async function requireLeaseInventory(directory,saved,audit,r){
// No inference about later commands, retries, observations or deliveries.
const allowed=["session.json","transport-audit.json","command-1",
"command-1.json","command-observed-1.json","ready-1.json"];
const u=r.unobserved;
if(u)allowed.push("command-2","command-2.json");
const names=await fs.readdir(directory);
const snapshot=await fs.readdir(directory+"/command-1");
if(names.length!==allowed.length||names.some(n=>!allowed.includes(n))||
snapshot.length!==1||snapshot[0]!=="prompt.txt")
throw Error("Unproven extra lease-session artifacts; preserve all evidence");
if(!u)return;
const files=await fs.readdir(directory+"/command-2");
if(files.length!==1||files[0]!=="prompt.txt")throw Error("Extra later-command evidence");
const command=await privateBytes(directory+"/command-2.json",4096);
const prompt=await privateBytes(directory+"/command-2/prompt.txt",4194304);
const {createHash}=await import("node:crypto");
const hash=b=>createHash("sha256").update(b).digest("hex");
if(hash(command)!==u.commandSha256||hash(prompt)!==u.promptSha256)
throw Error("Later-command pins differ");
new TextDecoder("utf-8",{fatal:true,ignoreBOM:true}).decode(prompt);
const v=JSON.parse(command.toString("utf8"));
if(!v||Object.keys(v).sort().join(",")!==
"clientSessionId,deadlineAt,nonce,ordinal,pid,ppid,promptSha256,requestId,sessionId"||
v.sessionId!==r.sessionId||v.ordinal!==2||v.requestId!==u.requestId||
v.requestId===r.requestId||typeof v.clientSessionId!=="string"||
!/^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/.test(v.clientSessionId)||
typeof v.nonce!=="string"||!/^[a-f0-9]{32}$/.test(v.nonce)||v.nonce===r.nonce||
v.promptSha256!==u.promptSha256||!Number.isSafeInteger(v.deadlineAt)||
v.deadlineAt<=audit.events[1].at||v.deadlineAt>saved.expiresAt||
Date.now()<v.deadlineAt||
!["pid","ppid"].every(k=>Number.isSafeInteger(v[k])&&v[k]>0))
throw Error("Unobserved ordinal-2 identity differs");
}

function terminalExpectation(t){
const {oldHelper,oldWorker,sentPromptSha256,descriptorSha256,auditSha256,unobserved,...r}=t??{};
resumeExpectation(r);
if(unobserved!==undefined&&(!unobserved||
Object.keys(unobserved).sort().join(",")!=="commandSha256,promptSha256,requestId"||
!validId(unobserved.requestId)||unobserved.requestId===r.requestId||
!["commandSha256","promptSha256"].every(k=>typeof unobserved[k]==="string"&&
/^[a-f0-9]{64}$/.test(unobserved[k]))))
throw Error("Exact later-command identity and pins required");
if(typeof oldHelper!=="string"||!oldHelper.startsWith("/")||oldHelper.includes("\0")||
!validId(oldWorker)||![sentPromptSha256,descriptorSha256,auditSha256].every(v=>
typeof v==="string"&&/^[a-f0-9]{64}$/.test(v)))
throw Error("Exact historical terminal expectations required");
}

async function terminalCheck(r,c){
const v=await cli(["queue","status",r.requestId],10000);
const a=(await cli(["status",r.requestId],10000)).assignment;
if(v.state!=="acknowledged"||v.dispatch_status!=="complete"||
v.sent_verified!==true||v.send_authorized!==false||v.send_may_have_occurred!==true||
v.request_id!==r.requestId||v.client_session_id!==r.clientSessionId||
v.fingerprint!==r.fingerprint||v.parent_task_id!==c.parent||
v.worker_conversation_id!==r.oldWorker||a?.status!=="complete"||
a.assignment_id!==r.requestId||a.parent_task_id!==c.parent||
a.worker_conversation_id!==r.oldWorker||a.submission_count!==1||
a.submission_observed!==true||a.outbound_prompt_verified!==true||
a.sent_prompt_sha256!==r.sentPromptSha256||a.wrapped_prompt_sha256!==r.sentPromptSha256||
a.result_marker_validated!==true||a.verification_level!=="bounded_native_summary"||
a.native_collection?.worker_id!==r.oldWorker)
throw Error("Canonical terminal receipt mismatch");
const missingPath=r.unobserved?
c.stateDir+"/assignments/"+r.unobserved.requestId+".json":null;
if(missingPath)await cli(["status",r.unobserved.requestId],10000,missingPath);
const s=await cli(["status","--current"],10000),q=await cli(["queue","status"],10000);
if(s.active_assignment!==null||s.active_cooldown!==null||
!Array.isArray(q.requests)||q.requests.some(v=>v.state==="claimed")||
s.worker?.conversation_id!==c.worker||s.worker?.model_confirmation!=="user-confirmed-pro"||
s.paths?.config_dir!==c.configDir||s.paths?.state_dir!==c.stateDir)
throw Error("Terminal replacement authority mismatch");
const rows=q.requests.filter(x=>x.request_id===r.requestId);
if(rows.length!==1||["state","dispatch_status","sent_verified","parent_task_id",
"worker_conversation_id"].some(k=>rows[0][k]!==v[k]))
throw Error("Ambiguous or changed terminal queue record");
if(missingPath){
if(q.requests.some(x=>typeof x.request_id!=="string"||
x.request_id===r.unobserved.requestId))throw Error("Later request exists or queue is ambiguous");
await absent(missingPath);
}
for(const p of [c.configDir,c.stateDir])
if(await fs.realpath(p)!==p)throw Error("Nonphysical authority");
await absent(c.stateDir+"/native-client");
return JSON.stringify([v,a]);
}

async function residentClosureCheck(directory,saved,audit,c,preclose=false){
const cancelled=audit.reason==="resident_start_cancelled";
if(saved.resident!==true||saved.leaseMs!==null||saved.expiresAt!==null||
(!cancelled&&audit.reason!=="resident_stopped")||audit.sessionId!==saved.sessionId||
!Array.isArray(audit.events)||audit.events.length%2!==0||(cancelled&&audit.events.length!==0))
throw Error("Not a clean resident closure");
const allowed=preclose?["session.json","wake.sock"]:["session.json","transport-audit.json","resident-stop","resident-stop.json"];
const receipts=[],seen=new Set();
if(cancelled){
try{
const raw=await privateBytes(directory+"/command-1.json",4096),v=JSON.parse(raw.toString("utf8"));
const prompt=await privateBytes(directory+"/command-1/prompt.txt",4194304);
const {createHash}=await import("node:crypto");
if(v.sessionId!==saved.sessionId||v.ordinal!==1||
typeof v.requestId!=="string"||!/^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/.test(v.requestId)||
!Number.isSafeInteger(v.deadlineAt)||v.deadlineAt>=Date.now()||
createHash("sha256").update(prompt).digest("hex")!==v.promptSha256||
(await fs.readdir(directory+"/command-1")).join(",")!=="prompt.txt")
throw Error("Unserved request evidence mismatch");
const missingPath=c.stateDir+"/assignments/"+v.requestId+".json";
await cli(["status",v.requestId],10000,missingPath);
await absent(missingPath);
const q=await cli(["queue","status"]);
if(!Array.isArray(q.requests)||q.requests.some(r=>r.request_id===v.requestId))
throw Error("Unserved request entered queue");
allowed.push("command-1","command-1.json");
}catch(e){
if(e.code!=="ENOENT")throw e;
// Only a completely absent command is eligible without command proof.
await absent(directory+"/command-1.json");
await absent(directory+"/command-1");
}
}
for(let i=0;i<audit.events.length;i+=2){
const a=audit.events[i],b=audit.events[i+1],n=i/2+1;
if(a.name!=="accepted"||a.operation!=="run"||b.name!=="finished"||
b.disposition!=="published"||a.callId!==b.callId||a.requestId!==b.requestId||
seen.has(a.requestId))throw Error("Unresolved resident delivery");
seen.add(a.requestId);
const base=directory+"/command-"+n;
const command=await privateBytes(base+".json",4096);
const observed=await privateBytes(directory+"/command-observed-"+n+".json",4096);
const ready=JSON.parse((await privateBytes(directory+"/ready-"+n+".json",4096)).toString("utf8"));
const v=JSON.parse(command.toString("utf8"));
const prompt=await privateBytes(base+"/prompt.txt",4194304);
const {createHash}=await import("node:crypto");
if(!command.equals(observed)||v.sessionId!==saved.sessionId||v.ordinal!==n||
v.requestId!==a.requestId||ready.sessionId!==saved.sessionId||ready.ordinal!==n||
createHash("sha256").update(prompt).digest("hex")!==v.promptSha256||
(await fs.readdir(base)).join(",")!=="prompt.txt")throw Error("Resident command evidence mismatch");
const assignment=(await cli(["status",a.requestId])).assignment;
const queued=await cli(["queue","status",a.requestId]);
const text=new TextDecoder("utf-8",{fatal:true,ignoreBOM:true}).decode(prompt);
const fingerprint=createHash("sha256").update(JSON.stringify([text,v.clientSessionId])
.replace(/[\u007f-\uffff]/g,ch=>"\\u"+ch.charCodeAt(0).toString(16).padStart(4,"0"))).digest("hex");
if(queued.fingerprint!==fingerprint)throw Error("Resident prompt differs from canonical request");
receipts.push(await terminalCheck({requestId:a.requestId,clientSessionId:v.clientSessionId,
fingerprint:queued.fingerprint,oldWorker:saved.worker,
sentPromptSha256:assignment?.sent_prompt_sha256},c));
allowed.push("command-"+n,"command-"+n+".json","command-observed-"+n+".json","ready-"+n+".json");
}
// A resident may be stopped after clients published commands but before the
// serving loop reached them. Preserve and prove every contiguous, expired,
// unobserved command instead of making that clean stop permanently
// unrecoverable. These commands never entered the canonical queue and are
// never replayed by the replacement listener.
for(let n=cancelled?2:audit.events.length/2+1;;n++){
const base=directory+"/command-"+n,commandPath=base+".json";
let command;
try{command=await privateBytes(commandPath,4096);}
catch(e){if(e.code==="ENOENT")break;throw e;}
try{
const v=JSON.parse(command.toString("utf8"));
const prompt=await privateBytes(base+"/prompt.txt",4194304);
const {createHash}=await import("node:crypto");
if(!v||Object.keys(v).sort().join(",")!==
"clientSessionId,deadlineAt,nonce,ordinal,pid,ppid,promptSha256,requestId,sessionId"||
v.sessionId!==saved.sessionId||v.ordinal!==n||
!["requestId","clientSessionId"].every(k=>typeof v[k]==="string"&&
/^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/.test(v[k]))||
!/^[a-f0-9]{32}$/.test(v.nonce)||
!/^[a-f0-9]{64}$/.test(v.promptSha256)||!Number.isSafeInteger(v.deadlineAt)||
v.deadlineAt>=Date.now()||
!["pid","ppid"].every(k=>Number.isSafeInteger(v[k])&&v[k]>0)||
createHash("sha256").update(prompt).digest("hex")!==v.promptSha256||
(await fs.readdir(base)).join(",")!=="prompt.txt")
throw Error("Unserved resident command evidence mismatch");
await absent(directory+"/ready-"+n+".json");
await absent(directory+"/command-observed-"+n+".json");
const missingPath=c.stateDir+"/assignments/"+v.requestId+".json";
await cli(["status",v.requestId],10000,missingPath);
await absent(missingPath);
const q=await cli(["queue","status"]);
if(!Array.isArray(q.requests)||q.requests.some(r=>r.request_id===v.requestId))
throw Error("Unserved resident request entered queue");
allowed.push("command-"+n,"command-"+n+".json");
}catch(e){
if(e.message==="Unserved resident request entered queue")throw e;
throw Error("Unproven resident artifacts; preserve evidence");
}
}
const names=await fs.readdir(directory);
if(names.length!==allowed.length||names.some(n=>!allowed.includes(n)))
throw Error("Unproven resident artifacts; preserve evidence");
if(!preclose){
const stop=JSON.parse((await privateBytes(directory+"/resident-stop.json",4096)).toString("utf8"));
if(stop.sessionId!==saved.sessionId||(await fs.readdir(directory+"/resident-stop")).length)
throw Error("Resident stop evidence mismatch");
}
const current=await cli(["status","--current"]),queue=await cli(["queue","status"]);
if(current.active_assignment!==null||current.active_cooldown!==null||
current.worker?.conversation_id!==c.worker||current.worker?.model_confirmation!=="user-confirmed-pro"||
current.paths?.config_dir!==c.configDir||current.paths?.state_dir!==c.stateDir||
!Array.isArray(queue.requests)||queue.requests.some(r=>r.state==="claimed"))
throw Error("Resident replacement authority changed");
return JSON.stringify(receipts);
}

export async function cancelUnstartedResident(g,meta){
const o=g.parkedResident,turn=meta?.["x-codex-turn-metadata"]?.turn_id;
function check(){
if(!o||o!==g.parkedResident||o.used!==false||o.socket!==g.parkedSocket||
o.binding!==g.parkedBinding||g.parkedDelivery!==null||
meta?.threadId!==o.binding?.broker||meta.threadId!==o.socket.config.parent||
typeof turn!=="string"||!turn||turn===o.binding.turn||
o.descriptor!==JSON.stringify(o.socket.config))throw Error("Not an interrupted unserved owner");
}
check();
if(g.parkedOpenBusy)throw Error("Native open already in progress");
g.parkedOpenBusy=true;
try{
const saved=JSON.parse((await privateBytes(o.directory+"/session.json",16384)).toString("utf8"));
if(JSON.stringify(saved)!==o.descriptor||saved.helper!==helper)
throw Error("Unserved descriptor mismatch");
const audit={sessionId:saved.sessionId,reason:"resident_start_cancelled",events:[]};
await residentClosureCheck(o.directory,saved,audit,saved,true);
check();
await publishResidentStop(o.directory,saved.sessionId);
await o.socket.close("resident_start_cancelled");
await absent(o.directory+"/wake.sock");
const actual=JSON.parse((await privateBytes(o.directory+"/transport-audit.json",16384)).toString("utf8"));
await residentClosureCheck(o.directory,saved,actual,saved);
return {closed:true,sessionId:saved.sessionId,reason:actual.reason};
}finally{g.parkedOpenBusy=false;}
}

async function privateDirectory(path){
const stat=await fs.lstat(path);
if(await fs.realpath(path)!==path||!stat.isDirectory()||stat.uid!==process.getuid()||
(stat.mode&0o077)!==0)throw Error("Physical owner-only client directory required");
}

async function clientPreflight(path){
await privateDirectory(path);
const probe=join(path,".pro-access-"+randomBytes(16).toString("hex"));
const h=await fs.open(probe,"wx",0o600);
try{await h.writeFile("client-access-check");await h.sync();
if(await fs.readFile(probe,"utf8")!=="client-access-check")throw Error("Client readback failed");
}finally{await h.close();await fs.unlink(probe);}
return {filesystemAccess:true,directory:path,sendAuthorized:false,
meaning:"Run in the actual Claude session. Does not prove rendezvous permission or native readiness."};
}

async function packet(broker,parent,worker,closedDirectory,resume,terminal,resident=false,sessionRoot){
if(sessionRoot!==undefined){
if(!resident)throw Error("Client directory is resident-only");
await privateDirectory(sessionRoot);
}
if(resident&&(broker!==parent||resume!==undefined))throw Error("Resident binding mismatch");
if(terminal!==undefined){
terminalExpectation(terminal);
if(resume!==undefined||closedDirectory===undefined||broker!==parent)
throw Error("Terminal replacement requires the same broker/parent and no resume");
}
if(![broker,parent,worker].every(v=>typeof v==="string"&&
/^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/.test(v)))
throw Error("Trusted IDs required");
const s=await cli(["status", "--current"]),q=await cli(["queue","status"]);
if(s.active_assignment!==null||s.active_cooldown!==null||
!Array.isArray(q.requests)||q.requests.some(r=>r.state==="claimed"))
throw Error("Authority occupied or unavailable");
if(s.worker?.conversation_id!==worker||
s.worker?.model_confirmation!=="user-confirmed-pro")
throw Error("Production worker mismatch");
for(const key of ["config_dir","state_dir"])
if(typeof s.paths?.[key]!=="string"||await fs.realpath(s.paths[key])!==s.paths[key])
throw Error("Nonphysical authority");
try{
await fs.lstat(join(s.paths.state_dir,"native-client"));
throw Error("Foreign state");
}catch(e){if(e.code!=="ENOENT") throw e;}
const trusted={
helper,configDir:s.paths.config_dir,stateDir:s.paths.state_dir,parent,worker,
leaseMs:7200000,idleMs:45000,replyMs:3900000,maxSnapshots:6,observationMs:50000,activeJobMs:3600000
};
if(resident)Object.assign(trusted,{resident:true,leaseMs:null});
if(resume!==undefined){
resumeExpectation(resume);
if(closedDirectory===undefined)throw Error("Closed queued session required");
requireQueuedCheck(await cli(queuedCheckArgs(resume,worker)),resume,trusted);
trusted.queuedResume=resume;
}
const recovery=resume??terminal;
let previous=null;
if(closedDirectory!==undefined){
if(typeof closedDirectory!=="string"||
await fs.realpath(closedDirectory)!==closedDirectory)
throw Error("Physical closed-session directory required");
const descriptor=await privateBytes(join(closedDirectory,"session.json"),16384);
const audit=await privateBytes(join(closedDirectory,"transport-audit.json"),16384);
const old=JSON.parse(descriptor.toString("utf8"));
const closed=JSON.parse(audit.toString("utf8"));
if(!old||!/^[a-f0-9]{32}$/.test(old.sessionId)||
!/^[a-f0-9]{48}$/.test(old.token)||old.helper!==(terminal?.oldHelper??helper)||
old.configDir!==trusted.configDir||old.stateDir!==trusted.stateDir||
old.parent!==parent||old.worker!==(terminal?.oldWorker??worker)||
closed?.sessionId!==old.sessionId)
throw Error("Prior session is not a matching closed, unused listener");
const evidenceSha256={};
if(recovery!==undefined){
const command=await privateBytes(join(closedDirectory,"command-1.json"),4096);
const observed=await privateBytes(join(closedDirectory,"command-observed-1.json"),4096);
const ready=await privateBytes(join(closedDirectory,"ready-1.json"),4096);
const prompt=await privateBytes(join(closedDirectory,"command-1/prompt.txt"),4194304);
const terminalLease=terminal!==undefined&&closed.reason==="lease_expired";
if(terminal?.unobserved!==undefined&&!terminalLease)
throw Error("Later-command proof requires lease-expired terminal mode");
requireClosedQueued(old,closed,JSON.parse(command.toString("utf8")),
JSON.parse(observed.toString("utf8")),JSON.parse(ready.toString("utf8")),
recovery,terminalLease);
if(terminalLease)await requireLeaseInventory(closedDirectory,old,closed,terminal);
for(const [name,raw] of [
["command-1.json",command],["command-observed-1.json",observed],
["ready-1.json",ready],["command-1/prompt.txt",prompt]
])evidenceSha256[name]=createHash("sha256").update(raw).digest("hex");
if(evidenceSha256["command-1/prompt.txt"]!==recovery.promptSha256)
throw Error("Closed-session private prompt snapshot differs");
}else if(resident&&["resident_stopped","resident_start_cancelled"].includes(closed.reason)){
await residentClosureCheck(closedDirectory,old,closed,trusted);
}else if(closed.reason!=="idle_expired"||
!Array.isArray(closed.events)||closed.events.length!==0){
throw Error("Prior session is not a matching closed, unused listener");
}
await absent(join(closedDirectory,"wake.sock"));
await absent(join(closedDirectory,"replacement-open.once.json"));
previous={
directory:closedDirectory,sessionId:old.sessionId,
descriptorSha256:createHash("sha256").update(descriptor).digest("hex"),
auditSha256:createHash("sha256").update(audit).digest("hex"),
...(resident&&["resident_stopped","resident_start_cancelled"].includes(closed.reason)?{residentClosed:true,unserved:closed.reason==="resident_start_cancelled"}:{}),
...(resume===undefined?{}:{queuedResume:resume,evidenceSha256})
};
if(terminal!==undefined){
if(previous.descriptorSha256!==terminal.descriptorSha256||
previous.auditSha256!==terminal.auditSha256)throw Error("Historical pins differ");
previous.terminal=terminal;
previous.evidenceSha256=evidenceSha256;
previous.terminalReceiptSha256=createHash("sha256")
.update(await terminalCheck(terminal,trusted)).digest("hex");
}
}
const openAttempt=randomBytes(16).toString("hex");
// Reuse the inspected private reader in the native REPL without node:process.
const privateReader=privateBytes.toString().replaceAll(
"ownerUid",String(process.getuid()));
const header='// @exec: {"yield_time_ms":60000}\n';
const call=code=>"await tools.mcp__node_repl__js("+J({
code,timeout_ms:60000,title:"Parked activation"
})+")";
const gate=`
const meta=nodeRepl.requestMeta;
if(meta?.threadId!==${J(broker)}||
meta?.["x-codex-turn-metadata"]?.turn_id!==parkedBinding.turn)
throw Error("Native broker task/turn changed");`;
const socket=join(dir,"parked-socket.mjs");
const open=`{
const meta=nodeRepl.requestMeta,turn=meta?.["x-codex-turn-metadata"]?.turn_id;
if(meta?.threadId!==${J(broker)}||typeof turn!=="string"||!turn)
throw Error("Broker identity mismatch");
const previous=${J(previous)},attempt=${J(openAttempt)};
const prior=globalThis.parkedSocket,binding=globalThis.parkedBinding;
const residentOwner=globalThis.parkedResident;
if(previous?.residentClosed&&residentOwner===undefined)throw Error("Missing resident owner");
if(${J(resident)}&&residentOwner!==undefined&&(!previous?.residentClosed||
residentOwner.socket!==prior||residentOwner.binding!==binding||residentOwner.used!==!previous.unserved||
residentOwner.directory!==previous.directory||residentOwner.descriptor!==JSON.stringify(prior.config)))
throw Error("Owner exists");
if(!previous&&(prior!==undefined||binding!==undefined||
globalThis.parkedDelivery!==undefined))throw Error("Do not reopen session");
if(previous&&(!prior||typeof prior.close!=="function"||
binding?.broker!==${J(broker)}||typeof binding.turn!=="string"||!binding.turn||
globalThis.parkedDelivery!==null))throw Error("Prior native binding is not recoverable");
if(globalThis.parkedOpenBusy)throw Error("Native open already in progress");
const attempts=globalThis.parkedOpenAttempts??=new Set();
if(!(attempts instanceof Set)||attempts.has(attempt)||
(!previous&&attempts.size!==0))throw Error("Open attempt already consumed");
attempts.add(attempt);
globalThis.parkedOpenBusy=true;
try{
const fs=await import("node:fs/promises"),crypto=await import("node:crypto");
const {dirname}=await import("node:path"),{constants}=await import("node:fs");
${privateReader}
${terminal===undefined&&!previous?.residentClosed?"":`const {execFile}=await import("node:child_process");
const helper=${J(helper)};
${cli.toString()}
${absent.toString()}
${terminalCheck.toString()}
${residentClosureCheck.toString()}`}
${requireClosedQueued.toString()}
${requireLeaseInventory.toString()}
async function missing(path){
try{await fs.lstat(path);}
catch(e){if(e.code==="ENOENT")return;throw e;}
throw Error("Expected absent recovery path: "+path);
}
if(crypto.createHash("sha256").update(await fs.readFile(${J(socket)})).digest("hex")
!==${J(pins["parked-socket.mjs"])}) throw Error("Socket pin changed");
if(previous){
const descriptor=await privateBytes(previous.directory+"/session.json",16384);
const raw=await privateBytes(previous.directory+"/transport-audit.json",16384);
const hash=b=>crypto.createHash("sha256").update(b).digest("hex");
if(hash(descriptor)!==previous.descriptorSha256||hash(raw)!==previous.auditSha256)
throw Error("Closed-session evidence changed");
const saved=JSON.parse(descriptor.toString("utf8")),audit=JSON.parse(raw.toString("utf8"));
if(saved.sessionId!==previous.sessionId||JSON.stringify(prior.config)!==JSON.stringify(saved)||
audit.sessionId!==saved.sessionId)
throw Error("Closed-session proof does not match retained native object");
const proof={};
async function verifyQueuedEvidence(){
const r=previous.queuedResume??previous.terminal;
if(!r)return;
for(const [name,expected] of Object.entries(previous.evidenceSha256)){
const value=await privateBytes(previous.directory+"/"+name,
name==="command-1/prompt.txt"?4194304:4096);
if(hash(value)!==expected)throw Error("Closed queued-session evidence changed");
if(name!=="command-1/prompt.txt")proof[name]=JSON.parse(value.toString("utf8"));
}
const terminalLease=previous.terminal!==undefined&&audit.reason==="lease_expired";
requireClosedQueued(saved,audit,proof["command-1.json"],
proof["command-observed-1.json"],proof["ready-1.json"],r,terminalLease);
if(terminalLease)await requireLeaseInventory(previous.directory,saved,audit,r);
if(previous.evidenceSha256["command-1/prompt.txt"]!==r.promptSha256)
throw Error("Closed queued-session prompt proof differs");
}
if(previous.residentClosed){
await residentClosureCheck(previous.directory,saved,audit,${J(trusted)});
}else if(previous.queuedResume||previous.terminal){
await verifyQueuedEvidence();
}else if(audit.reason!=="idle_expired"||
!Array.isArray(audit.events)||audit.events.length!==0){
throw Error("Closed-session proof does not match retained native object");
}
await missing(previous.directory+"/wake.sock");
await missing(previous.directory+"/replacement-open.once.json");
// Audit publication follows awaited server closure in the pinned socket code.
// Only after proving that closure, join its existing idempotent close promise.
await prior.close();
if(globalThis.parkedSocket!==prior||globalThis.parkedBinding!==binding||
globalThis.parkedDelivery!==null)
throw Error("Prior native state changed during recovery");
if(previous.terminal&&hash(await terminalCheck(previous.terminal,${J(trusted)}))
!==previous.terminalReceiptSha256)throw Error("Terminal receipt changed");
if(hash(await privateBytes(previous.directory+"/transport-audit.json",16384))!==previous.auditSha256)
throw Error("Closure evidence changed during recovery");
if(hash(await privateBytes(previous.directory+"/session.json",16384))!==previous.descriptorSha256)
throw Error("Descriptor changed during recovery");
await verifyQueuedEvidence();
await missing(previous.directory+"/wake.sock");
if(previous.residentClosed){
await residentClosureCheck(previous.directory,saved,audit,${J(trusted)});
if(globalThis.parkedResident!==residentOwner)throw Error("Resident owner changed");
}
if(previous.terminal&&(globalThis.parkedSocket!==prior||
globalThis.parkedBinding!==binding||globalThis.parkedDelivery!==null||
JSON.stringify(prior.config)!==JSON.stringify(saved)))
throw Error("Retained terminal state changed");
const marker=await fs.open(previous.directory+"/replacement-open.once.json","wx",0o600);
try{
await marker.writeFile(JSON.stringify({
attempt,previousSessionId:saved.sessionId,previousBinding:binding,
broker:${J(broker)},turn,auditSha256:previous.auditSha256,
...(previous.queuedResume?{
queuedResume:previous.queuedResume,evidenceSha256:previous.evidenceSha256
}:{}),
...(previous.terminal?{
terminal:previous.terminal,evidenceSha256:previous.evidenceSha256,
terminalReceiptSha256:previous.terminalReceiptSha256
}:{}),
meaning:"replacement attempt consumed; not proof of new listener success"
}),"utf8");
await marker.sync();
}finally{await marker.close();}
const parentDirectory=await fs.open(previous.directory,"r");
try{await parentDirectory.sync();}finally{await parentDirectory.close();}
}
const module=await import(${J(pathToFileURL(socket).href)});
const sessionRoot=${J(sessionRoot??null)}??nodeRepl.tmpDir;
${privateDirectory.toString().replaceAll("process.getuid()",String(process.getuid()))}
if(${J(sessionRoot!==undefined)})await privateDirectory(sessionRoot);
const tmp=await fs.mkdtemp(sessionRoot.replace(/\\/$/,"")+"/pro-session-");
await fs.chmod(tmp,0o700);
const directory=await fs.realpath(tmp);
let fresh;
try{fresh=await module.openSession(directory,${J(trusted)});}
catch(e){throw Error("Preserve failed session "+directory+": "+String(e));}
if(globalThis.parkedSocket!==prior||globalThis.parkedBinding!==binding||
(previous?.terminal&&globalThis.parkedDelivery!==null)){
await fresh.close("open_identity_changed");
throw Error("Native state changed; preserve new session "+directory);
}
globalThis.parkedBinding=Object.freeze({broker:${J(broker)},turn});
globalThis.parkedDelivery=null;
globalThis.parkedSocket=fresh;
if(${J(resident)})globalThis.parkedResident={
socket:fresh,binding:globalThis.parkedBinding,directory,attempt,used:false,
descriptor:JSON.stringify(fresh.config)
};
console.log(JSON.stringify({directory,sessionId:globalThis.parkedSocket.config.sessionId,
broker:${J(broker)},parent:${J(parent)},worker:${J(worker)},turn,
replacedSessionId:previous?.sessionId??null,phase:"listener_open_not_yet_waiting"}));
}finally{globalThis.parkedOpenBusy=false;}
}`;
const receive=`{${gate}
if(globalThis.parkedDelivery!==null) throw Error("Uncleared delivery");
globalThis.parkedDelivery=await parkedSocket.receive();
console.log(JSON.stringify({delivery:parkedDelivery}));
}`;
const held=`{${gate}
if(!parkedDelivery) throw Error("No accepted delivery");
console.log(JSON.stringify({delivery:parkedDelivery,sessionId:parkedSocket.config.sessionId}));
}`;
const clear=`{${gate}
globalThis.parkedDelivery=null;
console.log(JSON.stringify({cleared:true}));
}`;
const dispatch=sources["parked-runner.js"]+`
function activationValue(r){
if(!r||r.isError===true||!Array.isArray(r.content)||
r.content.length!==1||r.content[0].type!=="text")
throw Error("Invalid tool output");
return JSON.parse(r.content[0].text);
}
const held=activationValue(${call(held)});
const completed=await runParkedDelivery(
{...${J(trusted)},sessionId:held.sessionId,preflightConfirmed:true},held.delivery);
const transport=activationValue(completed.transport);
if(typeof transport.closed!=="boolean") throw Error("Unverified socket finish");
activationValue(${call(clear)});
text((${runnerReceipt.toString()})(completed.result));
`;
let openPreflight="";
const ownerGuard=`{${gate}
const o=globalThis.parkedResident;
if(o?.attempt!==${J(openAttempt)}||o.socket!==globalThis.parkedSocket||
o.binding!==globalThis.parkedBinding||JSON.stringify(o.socket.config)!==o.descriptor)
throw Error("Owner changed");
}`;
const quote=v=>"'"+String(v).replace(/'/g,"'\\''")+"'";
const residentCommand=[process.execPath,fileURLToPath(import.meta.url)]
.map(quote).join(" "),helperCommand=["python3",helper].map(quote).join(" ");
const serve=resident?`// @exec: {"yield_time_ms":1000}
const host=tools;
{
// Decode one text block; keep the undecodable envelope on the failure.
function value(r){
if(r?.isError===true||!Array.isArray(r?.content)||r.content.length!==1||
r.content[0].type!=="text"||typeof r.content[0].text!=="string")
throw Object.assign(Error("Invalid tool result"),{detail:r});
try{return JSON.parse(r.content[0].text);}
catch(e){throw Object.assign(Error("Invalid tool result"),{detail:r,cause:e});}
}
const guard=${J(ownerGuard)},quote=${quote.toString()};
async function native(code){
return value(await host.mcp__node_repl__js({
code:guard+code+guard,timeout_ms:60000,title:"Resident owner"
}));
}
async function checked(fn,a){await native('console.log("{}");');return await fn(a);}
const tools={
exec_command:a=>checked(x=>host.exec_command(x),a),
write_stdin:a=>checked(x=>host.write_stdin(x),a),
mcp__codex_app__read_thread:a=>checked(x=>host.mcp__codex_app__read_thread(x),a),
mcp__codex_app__send_message_to_thread:a=>checked(x=>host.mcp__codex_app__send_message_to_thread(x),a),
mcp__codex_app__navigate_to_codex_page:a=>checked(x=>host.mcp__codex_app__navigate_to_codex_page(x),a),
mcp__node_repl__js:a=>host.mcp__node_repl__js({...a,code:guard+a.code+guard})
};
${sources["parked-runner.js"].replace("globalThis.describeFailure =","const describeFailure =")
.replace("globalThis.runParkedJob =","const runParkedJob =")
.replace("globalThis.runParkedDelivery =","const runParkedDelivery =")}
// Service ends as resident_failed unless an explicit stop was observed with
// no delivery left unresolved. Failure wins over a simultaneous stop request.
// The invocation token binds serve ownership to this exact evaluation.
const invocation=Date.now().toString(36)+Math.random().toString(36).slice(2);
let claimed=false,pending=null,stopped=false,failure=null,owner=null,current=null;
async function command(args,executable=${J(residentCommand)}){
let r=await host.exec_command({
cmd:executable+" "+args.map(quote).join(" "),
login:false,tty:false,yield_time_ms:30000,max_output_tokens:4096
}),out="";
for(;;){
out+=r.output||"";
if(out.length>16384)throw Error("Oversize output");
if(r.exit_code!==undefined){
pending=null;
if(r.exit_code!==0)throw Error("Helper failed: "+out);
return JSON.parse(out);
}
if(pending===null){
if(!Number.isInteger(r.session_id))throw Error("Missing helper ID");
pending=r.session_id;
}
r=await host.write_stdin({session_id:pending,chars:"",
yield_time_ms:30000,max_output_tokens:4096});
}
}
// Ownership proof for cleanup and claim recovery: same parent, this exact
// invocation token, retained descriptor. It runs without the turn gate: a
// changed turn is itself a failure that still needs its socket closed.
const cleanup=async code=>value(await host.mcp__node_repl__js({
code:"{"+code+"}",timeout_ms:60000,title:"Resident close"}));
const ownerCode=\`const o=globalThis.parkedResident;
if(nodeRepl.requestMeta?.threadId!==${J(broker)}||o?.attempt!==${J(openAttempt)}||
o.used!==true||o.serveInvocation!==\${JSON.stringify(invocation)}||o.binding?.broker!==${J(broker)}||
JSON.stringify(o.socket.config)!==o.descriptor)
throw Error("Wrong cleanup owner");
const fs=await import("node:fs/promises");\`;
const identity='console.log(JSON.stringify({directory:o.directory,sessionId:o.socket.config.sessionId}));';
try{
try{
owner=await native(\`{
const o=globalThis.parkedResident;
if(o.used||globalThis.parkedDelivery!==null)throw Error("Serve consumed");
o.used=true;o.serveInvocation=\${JSON.stringify(invocation)};
\`+identity+'}');
}catch(e){
// The claim may have succeeded although its acknowledgment was lost. Only
// the invocation whose token the owner records cleans up; a duplicate serve
// never closes the running owner.
owner=await cleanup(ownerCode+identity).catch(()=>null);
if(owner===null)throw e;
claimed=true;
throw e;
}
claimed=true;
for(let ordinal=1;ordinal<=64;ordinal++){
const next=await command(["resident-next",owner.directory,ordinal]);
if(next.sessionId!==owner.sessionId)throw Error("Session mismatch");
if(next.stopped===true){stopped=true;break;}
await native('console.log("{}");');
const ready=await command(["command-ready",owner.directory,ordinal,next.command.requestId,
...(next.attempt?[next.attempt]:[])]);
if(ready.commandReady!==true||ready.sessionId!==owner.sessionId||ready.ordinal!==ordinal||
JSON.stringify(ready)!==JSON.stringify({commandReady:true,...next.command,meaning:ready.meaning}))
throw Error("Command changed");
const delivery=await native(\`{
const fs=await import("node:fs/promises"),o=globalThis.parkedResident;
try{await fs.lstat(o.directory+"/resident-stop.json");throw Error("Stop requested");}
catch(e){if(e.code!=="ENOENT")throw e;}
if(globalThis.parkedDelivery!==null||Date.now()>=\${JSON.stringify(ready.deadlineAt)})
throw Error("Delivery held or gate expired");
globalThis.parkedDelivery=await o.socket.receive();
console.log(JSON.stringify(globalThis.parkedDelivery));
}\`);
if(!delivery||delivery.sessionId!==owner.sessionId||
delivery.requestId!==ready.requestId||delivery.operation!=="run")
throw Error("Delivery/gate mismatch");
current=delivery.requestId;
const completed=await runParkedDelivery({
...${J(trusted)},sessionId:owner.sessionId,preflightConfirmed:true,restoreParent:false
},delivery);
// The runner result exists before its transport reply is decoded; capture
// its unresolved helper first, report the receipt even when that reply is
// undecodable, then fail the residence.
const result=completed.result,receipt=(${runnerReceipt.toString()})(result);
pending??=result?.pending_helper_session??null;
const terminal=["published","acknowledged"].includes(receipt.state);
let transport;
try{transport=value(completed.transport);}
catch(e){text(receipt);throw e;}
if(!terminal)text(receipt);
if(!terminal||result.ok!==true||result.pending_helper_session!=null||
!["native_navigation_returned","not_requested"].includes(result.restoration?.status)||
transport.closed!==false||transport.reservedRequestId!==null)
throw Object.assign(Error("Resident delivery unresolved; collect-only recovery required"),
{detail:{receipt,transport}});
await native(\`{
if(globalThis.parkedDelivery?.callId!==\${JSON.stringify(delivery.callId)})
throw Error("Delivery changed");
globalThis.parkedDelivery=null;
console.log("{}");
}\`);
current=null;
}
}catch(e){failure=e;}
finally{
if(claimed){
const report={kind:"resident_closed",outcome:null,transportReason:null,reconciliation:null,supplemental:[]};
const supplement=(step,e)=>report.supplemental.push({step,error:describeFailure(e)});
// 1. Join an unresolved helper execution before touching canonical state.
if(pending!==null)try{
const r=await host.write_stdin({session_id:pending,chars:"",yield_time_ms:30000,max_output_tokens:4096});
if(r.exit_code!==undefined)pending=null;else supplement("helper_drain",{message:"Helper still pending",detail:r});
}catch(e){supplement("helper_drain",e);}
// 2. Persist the failure summary unless this is a proven clean stop: observed
// explicit stop, no failure, no pending helper, no held delivery. The stop
// file is never published here; only a client publishes it.
const failurePath=owner.directory+"/resident-failure.json";
const summarize=error=>({sessionId:owner.sessionId,at:Date.now(),reason:"resident_failed",
stopRequested:stopped,pendingHelperSession:pending,requestId:current,error:describeFailure(error)});
const summary=stopped&&failure===null&&pending===null?null:
summarize(failure??Error("Resident service ended without an explicit stop"));
// REPL snippet: persist a non-null summary exclusively beside session.json.
const persist=\`if(summary!==null){
const h=await fs.open(\${JSON.stringify(failurePath)},"wx",0o600);
try{await h.writeFile(JSON.stringify({...summary,heldDelivery:globalThis.parkedDelivery}),"utf8");await h.sync();}
finally{await h.close();}
const d=await fs.open(o.directory,"r");
try{await d.sync();}finally{await d.close();}
}\`;
let held=null,recorded=false;
try{
const persisted=await cleanup(ownerCode+\`
let summary=\${JSON.stringify(summary)};
if(summary===null)try{
if(globalThis.parkedDelivery!==null)throw Error("Held delivery blocks a clean stop");
const stop=JSON.parse(await fs.readFile(o.directory+"/resident-stop.json","utf8"));
if(stop.sessionId!==o.socket.config.sessionId)throw Error("Stop evidence mismatch");
}catch(e){summary={sessionId:o.socket.config.sessionId,at:Date.now(),reason:"resident_failed",
stopRequested:true,pendingHelperSession:null,requestId:null,error:{name:e.name,message:String(e.message),stack:e.stack??null}};}
\`+persist+\`
console.log(JSON.stringify({outcome:summary===null?"resident_stopped":"resident_failed",
held:globalThis.parkedDelivery?.requestId??null}));\`);
report.outcome=persisted.outcome;held=persisted.held;recorded=persisted.outcome==="resident_failed";
}catch(e){report.outcome="resident_failed";supplement("failure_summary",e);}
// 3. Reconcile the captured request's canonical receipt through the existing
// locked indeterminate transition; completed receipts are preserved.
const rid=current??held;
if(report.outcome==="resident_failed"&&rid!==null){
if(pending!==null)report.reconciliation={requestId:rid,skipped:"helper_pending"};
else try{
const a=(await command(["status",rid],${J(helperCommand)})).assignment;
if(!a)report.reconciliation={requestId:rid,receipt:null};
else if(a.parent_task_id!==${J(parent)}||a.worker_conversation_id!==${J(worker)})
report.reconciliation={requestId:rid,receipt:a.status,skipped:"association_mismatch"};
else if(["armed","submitted","pending","ambiguous"].includes(a.status)){
const v=await command(["indeterminate",rid,"--reason-file",failurePath],${J(helperCommand)});
report.reconciliation={requestId:rid,receipt:v.assignment?.status??null,transition:"indeterminate",from:a.status};
}else report.reconciliation={requestId:rid,receipt:a.status,transition:null};
}catch(e){report.reconciliation={requestId:rid,skipped:"helper_failed"};supplement("reconciliation",e);}
}
// 4. Close the owned socket. Its first audit reason is immutable and may
// differ from the service outcome; report both. An unconfirmed close or
// audit is a failed service even after a clean stop.
try{
const closed=await cleanup(ownerCode+\`
await o.socket.close(\${JSON.stringify(report.outcome)});
console.log(JSON.stringify({closed:true,
transportReason:JSON.parse(await fs.readFile(o.directory+"/transport-audit.json","utf8")).reason}));\`);
report.transportReason=closed.transportReason;
}catch(e){
report.outcome="resident_failed";supplement("socket_close",e);
// A clean stop that fails to close is still a failed residence on disk.
if(!recorded)try{
await cleanup(ownerCode+"const summary="+JSON.stringify({...summarize(e),cleanupStep:"socket_close"})+";"+persist+'console.log("{}");');
}catch(e2){supplement("failure_summary",e2);}
}
text({...report,supplemental:report.supplemental.map(v=>({step:v.step,message:v.error?.message??null}))});
if(failure===null&&report.supplemental.length)failure=Object.assign(Error("Resident cleanup incomplete"),{detail:report.supplemental});
}
if(failure!==null)throw failure;
}
}
`:undefined;
if(resume!==undefined){
const quote=v=>"'"+String(v).replace(/'/g,"'\\''")+"'";
const cmd=["python3",helper,...queuedCheckArgs(resume,worker)].map(quote).join(" ");
openPreflight=requireQueuedCheck.toString()+"\n"+
"const eligibility=await tools.exec_command("+J({
cmd,login:false,tty:false,yield_time_ms:30000,max_output_tokens:4096
})+");\n"+
'if(eligibility.exit_code!==0)throw Error("Queued-resume check did not complete successfully; preserve helper execution "+(eligibility.session_id??"none"));\n'+
"requireQueuedCheck(JSON.parse(eligibility.output),"+J(resume)+","+J(trusted)+");\n";
}
return {kind:"native_activation_packet",authorization:"required_separately",
broker,parent,worker,trusted,pins,previous,openAttempt,calls:{
open:header+openPreflight+"text("+call(open)+");\n",
...(resident?{serve}:{receive:header+"text("+call(receive)+");\n",dispatch})
}};
}
async function ready(directory,ordinal){
if(!Number.isInteger(ordinal)||ordinal<1||ordinal>64)
throw Error("Invalid ready ordinal");
if((await fs.lstat(directory)).uid!==process.getuid()) throw Error("Wrong owner");
const {loadSession}=await import("./parked-socket.mjs");
const c=await loadSession(directory);
return await new Promise((resolve,reject)=>{
let done=false;
const watcher=watch(directory,()=>void check());
const timer=setTimeout(()=>end(Error("Readiness not observed")),25000);
function end(error,value){
if(done) return;
done=true;clearTimeout(timer);watcher.close();
error?reject(error):resolve(value);
}
async function check(){
if(done) return;
try{
if(c.resident!==true&&Date.now()>=c.expiresAt) throw Error("Session expired");
await absent(directory+"/transport-audit.json");
const v=JSON.parse(await fs.readFile(join(directory,"ready-"+ordinal+".json"),"utf8"));
if(v.sessionId!==c.sessionId||v.ordinal!==ordinal) throw Error("Wrong readiness");
end(null,{ready:true,sessionId:c.sessionId,ordinal,
meaning:"not an admission guarantee"});
}catch(e){if(e.code!=="ENOENT") end(e);}
}
watcher.on("error",e=>end(e));void check();
});
}
const validId=v=>typeof v==="string"&&
  /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/.test(v);

async function absent(path){
try{await fs.lstat(path);}
catch(e){if(e.code==="ENOENT")return;throw e;}
throw Error("Existing rendezvous artifact; do not reuse: "+path);
}

async function privateBytes(path,limit){
if(typeof path!=="string"||!path.startsWith("/")||
await fs.realpath(path)!==path)throw Error("Physical absolute input path required");
const parent=dirname(path),d=await fs.lstat(parent);
if(!d.isDirectory()||d.uid!==ownerUid||(d.mode&511)!==448)
throw Error("Input parent must be owned and mode 0700");
const h=await fs.open(path,constants.O_RDONLY|constants.O_NOFOLLOW|constants.O_NONBLOCK);
try{
const a=await h.stat();
if(!a.isFile()||a.uid!==ownerUid||(a.mode&511)!==384||a.nlink!==1)
throw Error("Input must be an owned, single-link mode-0600 regular file");
if(a.size>limit)throw Error("Input exceeds size bound");
const buffer=Buffer.alloc(limit+1);let used=0;
while(used<buffer.length){
const {bytesRead}=await h.read(buffer,used,buffer.length-used,null);
if(!bytesRead)break;used+=bytesRead;
}
const b=await h.stat(),current=await fs.lstat(path),after=await fs.lstat(parent);
const fields=["dev","ino","uid","mode","nlink","size","mtimeMs","ctimeMs"];
if(fields.some(k=>a[k]!==b[k]||b[k]!==current[k])||
d.dev!==after.dev||d.ino!==after.ino||d.mode!==after.mode||
d.uid!==after.uid||await fs.realpath(path)!==path)
throw Object.assign(Error("Input changed while reading"),{code:"EAGAIN"});
if(used>limit)throw Error("Input exceeds size bound");
return buffer.subarray(0,used);
}finally{await h.close();}
}

async function rendezvousSession(directory,ordinal,requestId){
if(!Number.isInteger(ordinal)||ordinal<1||ordinal>64||!validId(requestId))
throw Error("Invalid rendezvous identity");
if((await fs.lstat(directory)).uid!==process.getuid())throw Error("Wrong session owner");
const {loadSession}=await import("./parked-socket.mjs");
const c=await loadSession(directory);
if(!/^[a-f0-9]{32}$/.test(c.sessionId)||!/^[a-f0-9]{48}$/.test(c.token)||
(c.resident!==true&&(!Number.isSafeInteger(c.expiresAt)||Date.now()>=c.expiresAt))||
!Number.isInteger(c.idleMs)||c.idleMs<1||c.idleMs>50000||
!Number.isInteger(c.replyMs)||c.replyMs<1||c.replyMs>3900000)
throw Error("Invalid or expired session");
await absent(directory+"/transport-audit.json");
return c;
}

function waitRecord(directory,name,validate,ms){
let cancel;
const promise=new Promise((resolve,reject)=>{
let done=false;
const w=watch(directory,()=>void check());
const timer=setTimeout(()=>end(Error("Rendezvous deadline; do not retry")),ms);
function end(error,value){
if(done)return;done=true;clearTimeout(timer);w.close();
error?reject(error):resolve(value);
}
cancel=()=>end(Error("Rendezvous stopped"));
async function check(){
if(done)return;
try{
await absent(directory+"/transport-audit.json");
const value=JSON.parse((await privateBytes(directory+"/"+name,4096)).toString("utf8"));
validate(value);end(null,value);
}catch(e){
// Native ready files are written directly. Await another filesystem event
// if a read caught their creation/write; never start a timer polling loop.
if(e.code!=="ENOENT"&&e.code!=="EAGAIN"&&!(e instanceof SyntaxError))end(e);
}
}
w.on("error",e=>end(e));void check();
});
promise.catch(()=>{});
return {promise,cancel:()=>cancel?.()};
}

async function expiredCommand(directory,ordinal,requestId,c,attempt){
if(c.queuedResume!==undefined||typeof attempt!=="string"||
!/^[a-f0-9]{32}$/.test(attempt))
throw Error("Unobserved retry requires a fresh 32-hex attempt ID and no queued resume");
const v=JSON.parse((await privateBytes(
directory+"/command-"+ordinal+".json",4096)).toString("utf8"));
if(!v||Object.keys(v).sort().join(",")!==
"clientSessionId,deadlineAt,nonce,ordinal,pid,ppid,promptSha256,requestId,sessionId"||
v.sessionId!==c.sessionId||v.ordinal!==ordinal||v.requestId!==requestId||
!validId(v.clientSessionId)||!/^[a-f0-9]{32}$/.test(v.nonce)||
v.nonce===attempt||!/^[a-f0-9]{64}$/.test(v.promptSha256)||
!Number.isSafeInteger(v.deadlineAt)||v.deadlineAt<1||
(c.resident!==true&&v.deadlineAt>c.expiresAt)||Date.now()<v.deadlineAt||
!["pid","ppid"].every(k=>Number.isSafeInteger(v[k])&&v[k]>0))
throw Error("Retry requires the exact expired original command");
await absent(directory+"/ready-"+ordinal+".json");
await absent(directory+"/command-observed-"+ordinal+".json");
return v;
}

async function rendezvous(directory,ordinal,requestId,promptFile,clientSessionId,resume=false,attempt){
const c=await rendezvousSession(directory,ordinal,requestId);
const recovery=c.queuedResume;
if(recovery!==undefined)resumeExpectation(recovery);
if(resume){
if(!recovery||ordinal!==1||requestId!==recovery.requestId||c.sessionId===recovery.sessionId)
throw Error("Only the bound closed queued request may resume at ordinal 1");
clientSessionId=recovery.clientSessionId;
await absent(directory+"/queued-resume.once.json");
}else if(recovery&&(ordinal===1||requestId===recovery.requestId)){
throw Error("Use explicit queued resume, not another submission");
}
const original=attempt===undefined?null:
await expiredCommand(directory,ordinal,requestId,c,attempt);
if(original){
promptFile=directory+"/command-"+ordinal+"/prompt.txt";
clientSessionId=original.clientSessionId;
}
if(!validId(clientSessionId))throw Error("Invalid client session identity");
const raw=resume?null:await privateBytes(promptFile,4194304);
if(!resume)new TextDecoder("utf-8",{fatal:true,ignoreBOM:true}).decode(raw);
if(original&&createHash("sha256").update(raw).digest("hex")!==original.promptSha256)
throw Error("Original retry prompt differs");
if(!resume){
// Validate the same bytes we snapshot, before consuming any handoff artifact.
const check=await cli(["queue","check-submit","--request-id",requestId,
"--prompt-file","-","--client-session-id",clientSessionId],30000,undefined,raw);
if(check.input_valid!==true||check.send_authorized!==false)
throw Error("Missing input validation receipt");
}
const s=await cli(["status", "--current"]),q=await cli(["queue","status"]);
if(s.paths?.config_dir!==c.configDir||s.paths?.state_dir!==c.stateDir||
s.worker?.conversation_id!==c.worker)throw Error("Rendezvous authority mismatch");
if(s.active_assignment!==null||s.active_cooldown!==null||
!Array.isArray(q.requests)||q.requests.some(r=>r.state==="claimed"))
throw Error("Authority occupied; no command-ready signal");
if(resume){
if(c.helper!==helper)throw Error("Queued-resume helper differs");
requireQueuedCheck(await cli(queuedCheckArgs(recovery,c.worker)),recovery,c);
}else if(q.requests.some(r=>r.request_id===requestId)){
throw Error("Request already exists; collect its state, do not rendezvous again");
}
if(recovery&&!resume){
const prior=q.requests.find(r=>r.request_id===recovery.requestId);
if(!prior||!["published","acknowledged"].includes(prior.state)||
prior.dispatch_status!=="complete"||prior.sent_verified!==true||
prior.parent_task_id!==c.parent||prior.worker_conversation_id!==c.worker)
throw Error("Complete queued recovery before another request");
}
await absent(c.stateDir+"/native-client");
await absent(directory+"/ready-"+ordinal+".json");
await absent(directory+"/command-observed-"+ordinal+".json");
const name="command-"+ordinal+(attempt===undefined?"":"-retry-"+attempt);
await absent(directory+"/"+name+".json");
const ticket=directory+"/"+name; // Exclusive attempt marker; never remove to retry.
// A resident ticket is the claimed readiness of the waiter it publishes toward.
if(c.resident===true)await claimWaiter(directory,ordinal,c,ticket);
else await fs.mkdir(ticket,{mode:448});
const snapshot=ticket+"/prompt.txt";
if(!resume){
await fs.writeFile(snapshot,raw,{flag:"wx",mode:384});
await privateBytes(snapshot,4194304);
}
const pickupDeadline=Date.now()+c.idleMs;
const deadlineAt=c.resident===true?pickupDeadline:Math.min(c.expiresAt,pickupDeadline);
const record={
sessionId:c.sessionId,ordinal,requestId,clientSessionId,
nonce:attempt??randomBytes(16).toString("hex"),deadlineAt,
promptSha256:resume?recovery.promptSha256:createHash("sha256").update(raw).digest("hex"),
...(resume?{operation:"resume",fingerprint:recovery.fingerprint}:{}),
pid:process.pid,ppid:process.ppid
};
if(resume&&record.nonce===recovery.nonce)throw Error("Fresh resume nonce required");
const waiting=waitRecord(directory,"ready-"+ordinal+".json",v=>{
if(v.sessionId!==c.sessionId||v.ordinal!==ordinal||!Number.isFinite(v.at))
throw Error("Wrong native readiness");
if(Date.now()>=deadlineAt)throw Error("Command rendezvous expired");
},Math.max(1,deadlineAt-Date.now()));
try{
// Register the native-ready watcher before announcing command readiness.
await fs.writeFile(ticket+"/ready.tmp",J(record),{flag:"wx",mode:384});
await fs.rename(ticket+"/ready.tmp",directory+"/"+name+".json");
await waiting.promise;
await absent(directory+"/transport-audit.json");
// Every attempt shares one ordinal claim. Readiness alone grants nothing.
const observed=await privateBytes(directory+"/command-observed-"+ordinal+".json",4096);
if(observed.toString("utf8")!==J(record)||Date.now()>=deadlineAt)
throw Error("Command gate differs or expired; preserve attempt");
return await new Promise((resolve,reject)=>{
const child=execFile(process.execPath,[
join(dir,"parked-client.mjs"),directory,resume?"resume":"submit",requestId,
...(resume?[String(ordinal),record.nonce]:[snapshot,clientSessionId])
],{timeout:c.replyMs+180000,maxBuffer:8388608},(error,out,err)=>{
try{
const value=JSON.parse(error?err:out);
if(error||value.ok===false)throw Error(value.error||"Client failed; inspect existing request");
resolve(value);
}catch(e){reject(e);}
});
child.stdin?.end();
});
}finally{waiting.cancel();}
}

async function publishResidentStop(directory,sessionId){
const ticket=directory+"/resident-stop",path=directory+"/resident-stop.json";
await fs.mkdir(ticket,{mode:448}); // Permanent exclusive publication claim.
try{await fs.lstat(path);throw Error("Existing stop evidence");}
catch(e){if(e.code!=="ENOENT")throw e;}
const h=await fs.open(ticket+"/record.json","wx",384);
try{await h.writeFile(JSON.stringify({sessionId}));await h.sync();}
finally{await h.close();}
await fs.rename(ticket+"/record.json",path);
const d=await fs.open(directory,"r");
try{await d.sync();}finally{await d.close();}
}

async function residentStop(directory){
const c=await rendezvousSession(directory,1,"resident-stop");
if(c.resident!==true)throw Error("Not resident");
await publishResidentStop(directory,c.sessionId);
return {stopRequested:true,sessionId:c.sessionId};
}

// Readiness for ordinal N of one session is the empty owner-only directory
// waiting-N.<sessionId>, whose mtime the waiting resident-next keeps fresh.
// A rendezvous claims it by renaming it into its ticket; the waiter retires
// it with rmdir. Both are atomic, so exactly one side wins: a retired waiter
// leaves nothing to claim (no ticket exists), and a waiter that loses the
// race sees ENOENT and stays responsible for observing that publication. A
// killed waiter cannot retire; its marker goes stale within WAITER_FRESH_MS.
const WAITER_FRESH_MS=5000;
const waiterPath=(directory,ordinal,sessionId)=>directory+"/waiting-"+ordinal+"."+sessionId;
async function claimWaiter(directory,ordinal,c,ticket){
const marker=waiterPath(directory,ordinal,c.sessionId);
const gone=()=>Error("Resident is not waiting for ordinal "+ordinal+"; do not publish");
let stat;
try{stat=await fs.lstat(marker);}catch(e){if(e.code!=="ENOENT")throw e;throw gone();}
if(!stat.isDirectory()||stat.uid!==ownerUid||(stat.mode&511)!==448||Date.now()-stat.mtimeMs>WAITER_FRESH_MS)
throw Error("Stale resident readiness for ordinal "+ordinal+"; do not publish");
await absent(ticket);
try{await fs.rename(marker,ticket);}catch(e){if(e.code!=="ENOENT")throw e;throw gone();}
}

async function residentNext(directory,ordinal){
const c=await rendezvousSession(directory,ordinal,"resident-next");
if(c.resident!==true||c.helper!==helper)throw Error("Resident mismatch");
await absent(directory+"/ready-"+ordinal+".json");
await absent(directory+"/command-observed-"+ordinal+".json");
const marker=waiterPath(directory,ordinal,c.sessionId);
await fs.mkdir(marker,{mode:448});
const beat=setInterval(()=>fs.utimes(marker,new Date(),new Date()).catch(()=>{}),1000);
let claimed=false,choice;
async function retire(){
if(!claimed)try{await fs.rmdir(marker);}catch(e){if(e.code!=="ENOENT")throw e;claimed=true;}
return claimed;
}
try{
choice=await new Promise((resolve,reject)=>{
let done=false,busy=false,again=false,bound;
function end(e,v){if(done)return;done=true;clearTimeout(bound);w.close();e?reject(e):resolve(v);}
const w=watch(directory,()=>{again=true;void check();});
w.on("error",e=>end(e));
async function check(){
if(done||busy)return;busy=true;again=false;
try{
await absent(directory+"/transport-audit.json");
try{
const stop=JSON.parse((await privateBytes(directory+"/resident-stop.json",4096)).toString("utf8"));
if(Object.keys(stop).join(",")!=="sessionId"||stop.sessionId!==c.sessionId)
throw Error("Invalid stop");
// A claim that beat this stop holds the waiter here until its command appears.
if(!await retire()){end(null,{stopped:true});return;}
}catch(e){if(e.code!=="ENOENT")throw e;}
// The marker vanishing means a rendezvous claimed it (the watcher reports that
// rename); its command must follow within the bound or the residence fails
// with the claimed ticket kept as evidence.
if(!claimed)try{await fs.lstat(marker);}catch(e){if(e.code!=="ENOENT")throw e;claimed=true;}
if(claimed)bound??=setTimeout(()=>end(Error("Claimed readiness never published; preserve evidence")),WAITER_FRESH_MS);
const pattern=new RegExp("^command-"+ordinal+"(?:-retry-([a-f0-9]{32}))?[.]json$");
const found=[];
for(const name of await fs.readdir(directory)){
const match=pattern.exec(name);if(!match)continue;
const v=JSON.parse((await privateBytes(directory+"/"+name,4096)).toString("utf8"));
if(v.sessionId!==c.sessionId||v.ordinal!==ordinal||!validId(v.requestId)||
!Number.isSafeInteger(v.deadlineAt)||v.deadlineAt<1)
throw Error("Invalid command");
if(v.deadlineAt>Date.now())found.push({v,attempt:match[1]});
}
if(found.length>1)throw Error("Ambiguous commands");
if(found.length)end(null,found[0]);
}catch(e){end(e);}
finally{busy=false;if(again&&!done)void check();}
}
void check();
});
}finally{clearInterval(beat);await retire();}
if(choice.stopped)return {...choice,sessionId:c.sessionId};
const s=await cli(["status","--current"]),q=await cli(["queue","status"]);
if(s.paths?.state_dir!==c.stateDir||s.paths?.config_dir!==c.configDir||
s.worker?.conversation_id!==c.worker||s.worker?.model_confirmation!=="user-confirmed-pro"||
s.active_assignment!==null||s.active_cooldown!==null||!Array.isArray(q.requests)||
q.requests.some(r=>r.state==="claimed"||r.request_id===choice.v.requestId))
throw Error("Authority changed");
await absent(c.stateDir+"/native-client");
return {sessionId:c.sessionId,command:choice.v,attempt:choice.attempt};
}

async function commandReady(directory,ordinal,requestId,attempt){
const c=await rendezvousSession(directory,ordinal,requestId);
const recovery=c.queuedResume;
if(recovery!==undefined)resumeExpectation(recovery);
if(recovery&&ordinal===1&&requestId!==recovery.requestId)
throw Error("First command must resume the bound queued request");
const original=attempt===undefined?null:
await expiredCommand(directory,ordinal,requestId,c,attempt);
const consumed=directory+"/command-observed-"+ordinal+".json";
await absent(consumed);
const name="command-"+ordinal+(attempt===undefined?"":"-retry-"+attempt);
const waiting=waitRecord(directory,name+".json",v=>{
const now=Date.now();
if(v.sessionId!==c.sessionId||v.ordinal!==ordinal||v.requestId!==requestId||
!validId(v.clientSessionId)||!/^[a-f0-9]{32}$/.test(v.nonce)||
!/^[a-f0-9]{64}$/.test(v.promptSha256)||
!Number.isSafeInteger(v.deadlineAt)||v.deadlineAt<1||
(c.resident===true?v.deadlineAt>now+c.idleMs:v.deadlineAt>c.expiresAt)||
now>=v.deadlineAt)throw Error("Wrong or expired command readiness");
if(original&&(v.nonce!==attempt||v.clientSessionId!==original.clientSessionId||
v.promptSha256!==original.promptSha256))
throw Error("Retry command identity differs");
if(recovery&&ordinal===1){
if(v.operation!=="resume"||v.fingerprint!==recovery.fingerprint||
v.clientSessionId!==recovery.clientSessionId||v.promptSha256!==recovery.promptSha256||
v.nonce===recovery.nonce)throw Error("Wrong queued-resume command readiness");
}else if(v.operation!==undefined||v.fingerprint!==undefined){
throw Error("Unexpected resume command");
}
},c.resident===true?600000:Math.max(1,Math.min(600000,c.expiresAt-Date.now())));
try{
const value=await waiting.promise;
await absent(directory+"/transport-audit.json");
if(Date.now()>=value.deadlineAt)throw Error("Command gate expired");
// Original and retry gates compete for this same permanent ordinal claim.
await fs.writeFile(consumed,J(value),{flag:"wx",mode:384});
// A late write consumes the claim but must not grant a delayed receive.
if(Date.now()>=value.deadlineAt)throw Error("Command gate expired after claim; preserve evidence");
return {commandReady:true,...value,meaning:"start one receive; not send permission"};
}finally{waiting.cancel();}
}

if(typeof process!=="undefined"&&process.argv[1]&&await fs.realpath(process.argv[1])===fileURLToPath(import.meta.url))try{
const [action,...args]=process.argv.slice(2);
let result;
if(action==="packet"&&args.length===3) result=await packet(...args);
else if(action==="resident-packet"&&[3,4].includes(args.length))
result=await packet(...args.slice(0,3),undefined,undefined,undefined,true,args[3]);
else if(action==="closed-packet"&&args.length===4) result=await packet(...args);
else if(action==="closed-resident-packet"&&[4,5].includes(args.length))
result=await packet(...args.slice(0,4),undefined,undefined,true,args[4]);
else if(action==="client-preflight"&&args.length===1)
result=await clientPreflight(args[0]);
else if(action==="closed-queued-packet"&&args.length===11)
result=await packet(...args.slice(0,4),{
sessionId:args[4],requestId:args[5],fingerprint:args[6],callId:args[7],
clientSessionId:args[8],nonce:args[9],promptSha256:args[10]
});
else if(["closed-terminal-packet","closed-terminal-resident-packet"].includes(action)&&[16,19].includes(args.length))
result=await packet(...args.slice(0,4),undefined,{
oldHelper:args[4],oldWorker:args[5],sessionId:args[6],requestId:args[7],
callId:args[8],clientSessionId:args[9],fingerprint:args[10],nonce:args[11],
promptSha256:args[12],sentPromptSha256:args[13],
descriptorSha256:args[14],auditSha256:args[15],
...(args.length===19?{unobserved:{
requestId:args[16],commandSha256:args[17],promptSha256:args[18]
}}:{})
},action==="closed-terminal-resident-packet");
else if(action==="resident-next"&&args.length===2)
result=await residentNext(args[0],Number(args[1]));
else if(action==="resident-stop"&&args.length===1)
result=await residentStop(args[0]);
else if(action==="ready"&&args.length===2) result=await ready(args[0],Number(args[1]));
else if(action==="rendezvous"&&args.length===5)
result=await rendezvous(args[0],Number(args[1]),...args.slice(2));
else if(action==="rendezvous-retry"&&args.length===4)
result=await rendezvous(args[0],Number(args[1]),args[2],undefined,undefined,false,args[3]);
else if(action==="resume"&&args.length===3)
result=await rendezvous(args[0],Number(args[1]),args[2],undefined,undefined,true);
else if(action==="command-ready"&&[3,4].includes(args.length))
result=await commandReady(args[0],Number(args[1]),args[2],args[3]);
else throw Error("Invalid activation arguments");
console.log(J(result));
}catch(e){console.error(J({ok:false,error:String(e.message||e)}));process.exitCode=1;}
