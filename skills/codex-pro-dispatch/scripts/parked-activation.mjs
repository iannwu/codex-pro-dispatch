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
// A yielded exec cell is not a daemon. Its native calls belong to this owner
// turn. Carry the continuation obligation in both the packet and the first
// serve output, so the owner cannot mistake a startup receipt for completion.
const residentLifecycle=Object.freeze({
mode:"active_owner_turn",detachedSupported:false,
onYield:{tool:"functions.wait",cell_id:"the actual returned serve cell ID",yield_time_ms:60000},
instruction:"Do not send a final response while serve is running. Share the session path in commentary once, then automatically call functions.wait on the original returned cell ID after every yield, in this same owner turn, until serve completes. Do not ask for periodic confirmation, send routine progress messages, replay serve, or start a replacement. Waiting is execution supervision, not readiness or send authorization.",
onCompletion:"Verify the original serve outcome and cleanup evidence before final response. On failure preserve the cell ID, session and reservations; never infer permission to resend or replace."
});
// Worker gates accept exactly the two user-confirmation markers the helper
// writes; neither verifies a model. The literals stay inline because several
// gate functions are serialized into packets and cannot see module scope.
const pins={
  "parked-runner.js":"310e6c48254a116a7df0587dc39aea12a51d140f6f4f77d34bf99f2979539d3a",
"parked-socket.mjs":"7f14e2610e6254471272f0ae6c11aa2a0982979247122d81c13ee2a23f6f54d7",
"parked-client.mjs":"45b38c509bf9e12fb0cf6ddb323160c3e6edf9ea2aeff9025976b476b2c11072"
};
const sources={};
for(const [name,hash] of Object.entries(pins)){
const path=join(dir,name),raw=await fs.readFile(path);
if(await fs.realpath(path)!==path||
createHash("sha256").update(raw).digest("hex")!==hash)
throw Error("Pinned script mismatch: "+name);
sources[name]=raw.toString("utf8");
}
const servingPath=join(dir,"parked-serving.mjs"),servingHash="37e650554e74be1aed7fbbbb130db2adfe8f5efd344d7e3011074f97d54d2389";
if(await fs.realpath(servingPath)!==servingPath||createHash("sha256").update(await fs.readFile(servingPath)).digest("hex")!==servingHash)
throw Error("Pinned serving module mismatch");
const {createRunner,servePool,serveResident}=await import(pathToFileURL(servingPath).href+"?sha256="+servingHash);

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
!["user-confirmed-worker","user-confirmed-pro"].includes(v.worker_model_confirmation)||
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
s.worker?.conversation_id!==c.worker||
!["user-confirmed-worker","user-confirmed-pro"].includes(s.worker?.model_confirmation)||
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

async function privateDirectory(path){
const stat=await fs.lstat(path);
if(await fs.realpath(path)!==path||!stat.isDirectory()||stat.uid!==process.getuid()||
(stat.mode&0o077)!==0)throw Error("Physical owner-only client directory required");
}

// The canonical helper owns replacement. Session artifacts are evidence, not
// an expanding list of exceptions that grant authority to another execution.
export async function openResident(g,meta,trusted,expected,attempt,root){
const turn=meta?.["x-codex-turn-metadata"]?.turn_id;
if(meta?.threadId!==trusted.parent||typeof turn!=="string"||!turn)
throw Error("Native resident identity mismatch");
if(g.parkedOpenBusy)throw Error("Native open already in progress");
if(g.parkedSocket!==undefined&&!g.parkedResident?.credentials)
throw Error("Unfenced legacy owner requires quiescence proof");
g.parkedOpenBusy=true;
let fresh;
try{
const startCredentials={...expected,owner:attempt,parent:trusted.parent};
if(trusted.worker_pool_sha256){
startCredentials.worker_pool_sha256=trusted.worker_pool_sha256;
}else startCredentials.worker=trusted.worker;
const acquired=await cli(["resident","start",J(startCredentials)]);
if(!["ready","collect_only"].includes(acquired.state))return acquired;
const credentials=acquired.owner;
// Replacement already excludes new reservations by the old generation.
// Join only this runtime's old admission, never touch another runtime's files.
if(g.parkedResident){
await stopResidentAdmission(g.parkedResident);
await g.parkedResident.socket.close("owner_replaced");
}
const directory=await fs.realpath(await fs.mkdtemp(root.replace(/\/$/,"")+"/pro-session-"));
await fs.chmod(directory,448);
fresh=await (await import("./parked-socket.mjs")).openSession(directory,trusted);
await cli(["resident","bind-session",J({...credentials,session:{directory,
session_id:fresh.config.sessionId,descriptor_sha256:createHash("sha256").update(J(fresh.config)).digest("hex")}})]);
g.parkedSocket=fresh;g.parkedBinding=Object.freeze({broker:trusted.parent,turn});
g.parkedDelivery=null;
g.parkedResident={socket:fresh,binding:g.parkedBinding,directory,attempt,used:false,
descriptor:J(fresh.config),credentials,recover:acquired.request_id,recovery:acquired.request_ids||[],
preparedRecovery:acquired.prepared_ids||[],
recoveryBindings:acquired.recovery_bindings||{},
collectOnly:acquired.state==="collect_only",activation:{residentAdmission,stopResidentAdmission,recordResidentFailure,waitResidentStop,recordResidentJoined}};
return {state:acquired.state,directory,sessionId:fresh.config.sessionId,
credentials,request_id:acquired.request_id,phase:"listener_open_not_yet_waiting",
admissionObserved:false,lifecycle:residentLifecycle};
}catch(e){if(fresh)await fresh.close("open_failed");throw e;}
finally{g.parkedOpenBusy=false;}
}

export function poolRecoveryPlan(owner,capacity){
const collect=Array.isArray(owner?.recovery)?[...owner.recovery]:[];
const sendable=Array.isArray(owner?.preparedRecovery)?[...owner.preparedRecovery]:[];
if(collect.some(id=>sendable.includes(id)))
throw Error("Prepared recovery cannot be collector-only");
return capacity===undefined?{collect,sendable}:{collect,sendable,capacity};
}

export async function recoverPoolRequests(plan,hooks){
let pendingCollect=0;
if(plan.collect.length){
await hooks.openCollector();
try{
for(const request of plan.collect){
const result=await hooks.collect(request);
const status=result?.observation||result?.state;
// Collect-only ownership stays unresolved even when no native send is observed.
if(status==="pending"||status==="not_submitted"){
pendingCollect++;
continue;
}
if(result?.ok!==true||!["published","acknowledged"].includes(status))
throw Error("Recovery remains collect-only; preserve request");
await hooks.endCollected(request,result);
}
}finally{await hooks.closeCollector();}
}
for(const request of plan.sendable){
const result=await hooks.sendPrepared(request);
if(result?.observation==="not_submitted")
throw Error("Prepared recovery did not perform its eligible first send");
const status=result?.observation||result?.state;
if(result?.ok===true&&["published","acknowledged"].includes(status))
await hooks.endPrepared(request,result);
else if(status!=="pending")
throw Error("Prepared recovery unresolved; retain invocation");
}
if(pendingCollect>=(plan.capacity??1))
throw Error("Recovery remains collect-only; preserve request");
}

export async function waitResidentStop(owner){
const directory=owner?.directory,sessionId=owner?.socket?.config?.sessionId;
if(typeof directory!=="string"||typeof sessionId!=="string")
throw Error("Resident stop wait requires the bound session");
const waiting=waitRecord(directory,"resident-stop.json",v=>{
if(!v||Object.keys(v).join(",")!=="sessionId"||v.sessionId!==sessionId)
throw Error("Invalid stop");
},600000);
try{
await waiting.promise;
return {stopped:true};
}finally{waiting.cancel();}
}

function handoffOwner(g,meta,expected){
const o=g.parkedResident;
if(!o||meta?.threadId!==o.credentials?.parent||g.parkedSocket!==o.socket||
g.parkedBinding!==o.binding||!o.used||
!['generation','owner','parent','worker_pool_sha256'].every(k=>o.credentials[k]===expected[k]))
throw Error("Native handoff owner identity mismatch");
return o;
}

export async function recordResidentJoined(g,meta,attempt){
const o=handoffOwner(g,meta,g.parkedResident?.credentials??{});
if(o.attempt!==attempt||typeof o.serveInvocation!=="string"||!o.serveInvocation)
throw Error("Native serving invocation differs");
const audit=JSON.parse((await privateBytes(o.directory+"/transport-audit.json",1048576)).toString("utf8"));
if(audit.sessionId!==o.socket.config.sessionId||audit.reason!=="resident_stopped")
throw Error("Graceful serving completion required");
await absent(o.directory+"/wake.sock");
if((await fs.readdir(o.directory)).some(n=>n.startsWith("waiting-")))throw Error("Waiter still active");
const joined={generation:o.credentials.generation,owner:o.credentials.owner,parent:o.credentials.parent,
sessionId:o.socket.config.sessionId,invocation:o.serveInvocation};
const marker=await fs.open(o.directory+"/resident-joined.json","wx",384);
try{await marker.writeFile(J(joined));await marker.sync();}finally{await marker.close();}
const directory=await fs.open(o.directory,"r");
try{await directory.sync();}finally{await directory.close();}
o.serveJoined=Object.freeze(joined);
return {kind:"resident_joined",sessionId:joined.sessionId,sendAuthorized:false};
}

function joinedServe(code,attempt){
// Only this continuation can write the barrier, after the whole serving
// function (including its finally/cleanup) has returned successfully.
return `// @exec: {"yield_time_ms":1000}
await (async()=>{\n${code}\n})();
const joined=await tools.mcp__node_repl__js(${J({code:`console.log(JSON.stringify(await globalThis.parkedResident.activation.recordResidentJoined(globalThis,nodeRepl.requestMeta,${J(attempt)})));`,timeout_ms:60000,title:"Record joined resident execution"})});
if(joined?.isError===true)throw Error("Native join barrier failed");
text(joined);`;
}

export async function verifyHandoffContext(g,meta,expected){
const o=handoffOwner(g,meta,expected);
if(!o.serveJoined)throw Error("Original serving function has not joined");
const raw=await privateBytes(o.directory+"/resident-joined.json",16384);
if(raw.toString("utf8")!==J(o.serveJoined))throw Error("Native join barrier changed");
return {parent:o.credentials.parent,sessionId:o.socket.config.sessionId};
}

export async function commitNativeHandoff(g,meta,expected,target){
await verifyHandoffContext(g,meta,expected);
if(target?.schemaVersion!==1||target.thread?.kind!=="codex"||target.thread?.hostId!=="local"||
target.thread?.id!==expected.new_parent)throw Error("Replacement native task was not verified");
const o=g.parkedResident;
// There is deliberately no public CLI commit path. This private bridge runs
// only after the native gate. It uses the existing Python lock and validator.
const code=`import json,sys\nfrom pathlib import Path\nsys.path.insert(0,${J(join(dir,"../../../src"))})\nfrom codex_pro_dispatch import core,resident\np=json.load(sys.stdin)\npaths=core.RuntimePaths(Path(p['config']),Path(p['state']))\nprint(json.dumps(resident._handoff_from_native(paths,p['credentials'])))`;
return await new Promise((resolve,reject)=>{
const child=execFile("python3",["-c",code],{timeout:30000,maxBuffer:1048576},(error,out,err)=>{
if(error)return reject(Error(err||"Native handoff commit failed"));
try{resolve(JSON.parse(out));}catch(e){reject(e);}
});
child.stdin.on("error",reject);
child.stdin.end(J({config:o.socket.config.configDir,state:o.socket.config.stateDir,credentials:expected}));
});
}

export function buildHandoffCall(expected,sourceHash){
if(!/^[a-f0-9]{64}$/.test(sourceHash))throw Error("Pinned native handoff source required");
const modulePath=join(dir,"parked-activation.mjs");
const moduleUrl=pathToFileURL(modulePath).href+"?sha256="+sourceHash;
const header=`const fs=await import("node:fs/promises"),crypto=await import("node:crypto");
if(await fs.realpath(${J(modulePath)})!==${J(modulePath)}||crypto.createHash("sha256").update(await fs.readFile(${J(modulePath)})).digest("hex")!==${J(sourceHash)})throw Error("Activation pin changed");
const a=await import(${J(moduleUrl)});`;
const native=code=>J({code,timeout_ms:60000,title:"Native listener handoff"});
return `function value(r){if(r?.isError||r?.content?.length!==1||r.content[0].type!=="text")throw Error("Invalid native handoff result");return JSON.parse(r.content[0].text);}
value(await tools.mcp__node_repl__js(${native(`{${header}console.log(JSON.stringify(await a.verifyHandoffContext(globalThis,nodeRepl.requestMeta,${J(expected)})));}`)}));
const target=value(await tools.mcp__codex_app__read_thread({threadId:${J(expected.new_parent)},turnLimit:1,includeOutputs:false}));
if(target.schemaVersion!==1||target.thread?.id!==${J(expected.new_parent)}||target.thread?.kind!=="codex"||target.thread?.hostId!=="local")throw Error("Replacement native task does not exist locally");
const checked={schemaVersion:1,thread:{id:target.thread.id,kind:target.thread.kind,hostId:target.thread.hostId}};
text(value(await tools.mcp__node_repl__js({code:'{'+${J(header)}+'console.log(JSON.stringify(await a.commitNativeHandoff(globalThis,nodeRepl.requestMeta,'+${J(J(expected))}+','+JSON.stringify(checked)+')));}',timeout_ms:60000,title:"Commit native listener handoff"})));`;
}

async function handoffPacket(newParent){
if(typeof newParent!=="string"||!/^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/.test(newParent))
throw Error("Native replacement task ID required");
const v=(await cli(["resident","inspect"])).owner;
if(v?.version!==3||v.parent===newParent)throw Error("Different replacement task and schema-3 owner required");
const expected={generation:v.generation,owner:v.owner,parent:v.parent,worker_pool_sha256:v.worker_pool_sha256,new_parent:newParent};
const sourceHash=createHash("sha256").update(await fs.readFile(fileURLToPath(import.meta.url))).digest("hex");
return {kind:"native_handoff_packet",sendAuthorized:false,calls:{handoff:buildHandoffCall(expected,sourceHash)}};
}

export async function captureTakeoverIdentity(g,meta,expected){
const replacement=meta?.threadId,turn=meta?.["x-codex-turn-metadata"]?.turn_id;
if(typeof replacement!=="string"||!/^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/.test(replacement)||
typeof turn!=="string"||!turn)throw Error("identity_invalid");
if(replacement===expected?.parent)return {outcome:"already_owner"};
g.parkedTakeover=Object.freeze({replacement,turn,expected});
return {replacement_task_id:replacement};
}

export async function commitNativeTakeover(g,meta,expected,raw,tmpDir){
const saved=g.parkedTakeover,turn=meta?.["x-codex-turn-metadata"]?.turn_id;
if(!saved||saved.replacement!==meta?.threadId||saved.turn!==turn||J(saved.expected)!==J(expected))
throw Error("identity_invalid");
if(typeof raw!=="string")throw Error("old_owner_unreadable");
if(typeof tmpDir!=="string"||!tmpDir.startsWith("/"))throw Error("identity_invalid");
const directory=await fs.mkdtemp(tmpDir.replace(/\/$/,"")+"/pro-takeover-");
await fs.chmod(directory,448);
const evidence=join(directory,"owner-read.json");
const file=await fs.open(evidence,"wx",384);
try{await file.writeFile(raw,"utf8");await file.sync();}finally{await file.close();}
const folder=await fs.open(directory,"r");try{await folder.sync();}finally{await folder.close();}
const code=`import json,sys\nfrom pathlib import Path\nsys.path.insert(0,${J(join(dir,"../../../src"))})\nfrom codex_pro_dispatch import core,resident\np=json.load(sys.stdin)\npaths=core.RuntimePaths(Path(p['config']),Path(p['state']))\nprint(json.dumps(resident._takeover_from_native(paths,p['packet'])))`;
return await new Promise(resolve=>{
const child=execFile("python3",["-c",code],{timeout:30000,maxBuffer:1048576},(error,out,err)=>{
if(error){resolve({ok:false,outcome:"commit_unknown",error:String(err||error.message||error)});return;}
try{resolve(JSON.parse(out));}catch(e){resolve({ok:false,outcome:"commit_unknown",error:String(e.message||e)});}
});
child.stdin.on("error",e=>resolve({ok:false,outcome:"commit_unknown",error:String(e.message||e)}));
child.stdin.end(J({config:expected.configDir,state:expected.stateDir,packet:{schema_version:1,replacement_task_id:saved.replacement,
expected:{generation:expected.generation,owner:expected.owner,parent:expected.parent,worker_pool_sha256:expected.worker_pool_sha256},owner_read_text:raw,evidence_path:evidence}}));
});
}

export function buildTakeoverCall(expected,sourceHash){
if(!/^[a-f0-9]{64}$/.test(sourceHash))throw Error("Pinned native takeover source required");
const modulePath=join(dir,"parked-activation.mjs"),moduleUrl=pathToFileURL(modulePath).href+"?sha256="+sourceHash;
const header=`const fs=await import("node:fs/promises"),crypto=await import("node:crypto");
if(await fs.realpath(${J(modulePath)})!==${J(modulePath)}||crypto.createHash("sha256").update(await fs.readFile(${J(modulePath)})).digest("hex")!==${J(sourceHash)})throw Error("Activation pin changed");
const a=await import(${J(moduleUrl)});`;
const native=(code,title)=>J({code,timeout_ms:60000,title});
return `function value(r){if(r?.isError||r?.content?.length!==1||r.content[0].type!=="text")throw Error("identity_invalid");return JSON.parse(r.content[0].text);}
try{
const identity=value(await tools.mcp__node_repl__js(${native(`{${header}console.log(JSON.stringify(await a.captureTakeoverIdentity(globalThis,nodeRepl.requestMeta,${J(expected)})));}`,"Capture takeover identity")}));
if(identity.outcome==="already_owner"){text(identity);}else{
let read;
try{read=await tools.mcp__codex_app__read_thread({threadId:${J(expected.parent)},turnLimit:1,includeOutputs:false});}
catch(e){read={isError:true,error:String(e?.message||e)};}
const exactKeys=(v,keys)=>v!==null&&typeof v==="object"&&!Array.isArray(v)&&Object.keys(v).sort().join(",")===keys;
const deleted=exactKeys(read,"content,isError")&&read.isError===true&&Array.isArray(read.content)&&read.content.length===1&&
exactKeys(read.content[0],"text,type")&&read.content[0].type==="text"&&
read.content[0].text===${J("No Codex thread found for threadId: "+expected.parent+". Hosts without a readable match: local")};
let unreadable=read?.isError||read?.truncated===true||read?.textTruncated===true||read?.content?.length!==1||read.content[0]?.type!=="text"||typeof read.content[0]?.text!=="string";
if(!unreadable){try{const parsed=JSON.parse(read.content[0].text);if(parsed&&Object.hasOwn(parsed,"owner_read_failure"))unreadable=true;}catch{unreadable=true;}}
if(!deleted&&unreadable){text({ok:false,outcome:"old_owner_unreadable",send_authorized:false});}else{
const raw=deleted?JSON.stringify({owner_read_failure:read}):read.content[0].text;
const commitCode='{'+${J(header)}+'console.log(JSON.stringify(await a.commitNativeTakeover(globalThis,nodeRepl.requestMeta,'+${J(J(expected))}+','+JSON.stringify(raw)+',nodeRepl.tmpDir)));}';
text(value(await tools.mcp__node_repl__js({code:commitCode,timeout_ms:60000,title:"Commit takeover"})));}}}catch(e){if(!String(e?.message||e).includes("identity_invalid"))throw e;text({ok:false,outcome:"identity_invalid",send_authorized:false});}`;
}

async function takeoverPacket(){
const status=await cli(["status","--current"]),v=(await cli(["resident","inspect"])).owner;
if(v?.version!==3)throw Error("Takeover requires a schema-3 resident owner");
const expected={generation:v.generation,owner:v.owner,parent:v.parent,worker_pool_sha256:v.worker_pool_sha256,
configDir:status.paths?.config_dir,stateDir:status.paths?.state_dir};
if(typeof expected.configDir!=="string"||typeof expected.stateDir!=="string")throw Error("Current status lacks canonical paths");
const sourceHash=createHash("sha256").update(await fs.readFile(fileURLToPath(import.meta.url))).digest("hex");
return {kind:"native_takeover_packet",sendAuthorized:false,expected,calls:{takeover:buildTakeoverCall(expected,sourceHash)}};
}

// This path retains the original native object and socket. It cannot recreate
// a lost runtime. Failure after the synchronous fence is permanently consumed.
export async function claimServeExisting(g,meta,expected,token){
const o=g.parkedResident,turn=meta?.["x-codex-turn-metadata"]?.turn_id;
const binding=o?.binding,socket=o?.socket;
if(!o||!["socket","binding","directory","attempt","used","descriptor","credentials",
"recover","recovery","preparedRecovery","recoveryBindings","collectOnly","activation"].every(k=>Object.hasOwn(o,k))||
g.parkedOpenBusy!==false||g.parkedSocket!==o.socket||
g.parkedBinding!==o.binding||g.parkedDelivery!==null||
meta?.threadId!==expected.parent||o.binding?.broker!==expected.parent||
typeof turn!=="string"||!turn||
typeof o.binding.turn!=="string"||!o.binding.turn||
o.used!==false||o.serveInvocation!==undefined||o.serveExistingClaim!==undefined||
o.serveExistingRelay!==undefined||o.unusedReplacement!==undefined||
o.admission!==undefined||o.failureRecord!==undefined||o.failureFinalRecord!==undefined||
o.serveJoined!==undefined||o.collectOnly!==false||o.recover!=null||
!Array.isArray(o.recovery)||o.recovery.length||
!Array.isArray(o.preparedRecovery)||o.preparedRecovery.length||
!o.recoveryBindings||typeof o.recoveryBindings!=="object"||Array.isArray(o.recoveryBindings)||
Object.keys(o.recoveryBindings).length||
!["residentAdmission","stopResidentAdmission","recordResidentFailure","waitResidentStop","recordResidentJoined"].every(k=>typeof o.activation?.[k]==="function")||
!o.socket||typeof o.socket.receive!=="function"||typeof o.socket.close!=="function"||
J(o.socket.config)!==o.descriptor||o.directory!==expected.session.directory||
o.socket.config.sessionId!==expected.session.session_id||
o.attempt!==expected.owner||
!["generation","owner","parent",...(expected.worker_pool_sha256?["worker_pool_sha256"]:["worker"])].every(k=>o.credentials?.[k]===expected[k])||
createHash("sha256").update(o.descriptor).digest("hex")!==expected.session.descriptor_sha256)
throw Error("Serve-existing native proof failed; preserve session");
// No await before fencing: a second recovery or original serve cannot race us.
o.used=true;o.serveExistingClaim=token;
await cli(["resident","claim-serve-existing",J(expected)]);
// Open creates no receive waiter. The pinned socket only records request
// events after receive(), which permanently writes ready-N. The canonical
// claim checks the exact pristine inventory, including absence of ready-N.
if(g.parkedResident!==o||g.parkedSocket!==socket||o.socket!==socket||g.parkedBinding!==binding||o.binding!==binding||
meta?.threadId!==expected.parent||meta?.["x-codex-turn-metadata"]?.turn_id!==turn||
o.used!==true||o.serveExistingClaim!==token||o.serveInvocation!==undefined||o.admission!==undefined||
g.parkedDelivery!==null||J(o.socket.config)!==o.descriptor)
throw Error("Serve-existing native proof changed; preserve claim");
// Only a successfully fenced, pristine same-task claim may bind a later turn.
// The original frozen binding is never mutated; all subsequent calls must
// match the new turn, and a lost reply still leaves this claim consumed.
if(turn!==binding.turn)o.binding=g.parkedBinding=Object.freeze({broker:expected.parent,turn});
o.serveExistingRelay={token,binding:o.binding,socket:o.socket,expected,started:false};
return {claimed:true};
}

// A retained claim owns one continuation on the native object. Module imports
// may be fresh on every host call. Replies settle each emitted
// host call once. Lost/duplicate replies cannot restart or recreate that loop.
export async function replaceUnusedServing(g,meta,expected,token){
const o=g.parkedResident,turn=meta?.["x-codex-turn-metadata"]?.turn_id;
if(!o||meta?.threadId!==expected.parent||o.binding?.broker!==expected.parent||
!["socket","binding","directory","attempt","used","descriptor","credentials","recover","recovery","preparedRecovery","recoveryBindings","collectOnly","activation"].every(k=>Object.hasOwn(o,k))||
typeof turn!=="string"||!turn||g.parkedBinding!==o.binding||
g.parkedSocket!==o.socket||g.parkedOpenBusy!==false||g.parkedDelivery!==null||
o.used!==true||typeof o.serveExistingClaim!=="string"||!o.serveExistingClaim||
o.unusedReplacement!==undefined||o.serveInvocation!==undefined||
o.admission!==undefined||o.failureRecord!==undefined||o.failureFinalRecord!==undefined||
o.serveJoined!==undefined||o.collectOnly!==false||o.recover!=null||
!Array.isArray(o.recovery)||o.recovery.length||!Array.isArray(o.preparedRecovery)||o.preparedRecovery.length||
!o.recoveryBindings||typeof o.recoveryBindings!=="object"||Array.isArray(o.recoveryBindings)||Object.keys(o.recoveryBindings).length||
(o.serveExistingRelay!==undefined&&(!o.serveExistingRelay||o.serveExistingRelay.started!==false||
o.serveExistingRelay.token!==o.serveExistingClaim||o.serveExistingRelay.socket!==o.socket||o.serveExistingRelay.binding!==o.binding))||
typeof o.socket?.close!=="function"||typeof o.socket?.receive!=="function"||
J(o.socket.config)!==o.descriptor||o.directory!==expected.session.directory||
o.socket.config.sessionId!==expected.session.session_id||o.attempt!==expected.owner||
!["generation","owner","parent",...(expected.worker_pool_sha256?["worker_pool_sha256"]:["worker"])].every(k=>o.credentials?.[k]===expected[k])||
createHash("sha256").update(o.descriptor).digest("hex")!==expected.session.descriptor_sha256)
throw Error("Unused replacement native proof failed; preserve session");
const socket=o.socket,binding=o.binding;
// Synchronous permanent fence also rejects a delayed original serve body.
o.unusedReplacement=token;o.serveInvocation="unused-replacement:"+token;
const result=await cli(["resident","replace-unused-serving",J(expected)]);
if(result?.replaced!==true||g.parkedResident!==o||g.parkedSocket!==socket||
o.socket!==socket||g.parkedBinding!==binding||o.binding!==binding||
meta?.threadId!==expected.parent||meta?.["x-codex-turn-metadata"]?.turn_id!==turn)
throw Error("Unused replacement result uncertain; preserve evidence");
await socket.close("unused_serving_replaced");
return {replaced:true,sendAuthorized:false};
}

function retainedRelay(g,meta,token){
const o=g.parkedResident,r=o?.serveExistingRelay;
if(!r||r.token!==token||o.serveExistingClaim!==token||o.used!==true||
o.unusedReplacement!==undefined||r.continuing!==undefined||
g.parkedSocket!==r.socket||o.socket!==r.socket||g.parkedBinding!==r.binding||o.binding!==r.binding||
meta?.threadId!==r.expected.parent||meta?.["x-codex-turn-metadata"]?.turn_id!==r.binding.turn)
throw Error("Serve-existing relay identity changed; preserve claim");
return r;
}

// Consume before crossing the outer host boundary. A lost send reply never
// makes that call dispatchable again, including across turn changes.
export function claimRelayCall(g,meta,token,id){
const r=retainedRelay(g,meta,token),call=r.pending?.get(id);
if(!call||call.dispatched)throw Error("Relay call already consumed or unknown");
call.dispatched=true;
return call.request;
}

export async function continueServeExisting(g,meta,expected,token){
const o=g.parkedResident,r=o?.serveExistingRelay;
const turn=meta?.["x-codex-turn-metadata"]?.turn_id;
const binding=o?.binding;
if(!r||!r.started||r.done||r.error||r.continuing!==undefined||
!expected.worker_pool_sha256||J(r.expected)!==J(expected)||
o.used!==true||o.serveExistingClaim!==r.token||o.unusedReplacement!==undefined||
o.failureRecord!==undefined||o.failureFinalRecord!==undefined||o.serveJoined!==undefined||
g.parkedOpenBusy!==false||g.parkedDelivery!==null||
meta?.threadId!==expected.parent||binding?.broker!==expected.parent||
typeof turn!=="string"||!turn||turn===binding.turn||r.retiredTurns?.has(turn)||
g.parkedBinding!==binding||r.binding!==binding||
g.parkedSocket!==o.socket||r.socket!==o.socket||
o.attempt!==expected.owner||o.directory!==expected.session.directory||
o.socket.config.sessionId!==expected.session.session_id||J(o.socket.config)!==o.descriptor||
createHash("sha256").update(o.descriptor).digest("hex")!==expected.session.descriptor_sha256||
!["generation","owner","parent","worker_pool_sha256"].every(k=>o.credentials?.[k]===expected[k])||
typeof o.serveInvocation!=="string"||!o.serveInvocation||
!(r.pending instanceof Map)||r.pending.size<1||r.pending.size>2)
throw Error("Post-arm continuation proof failed; preserve relay");
// This is the exact read-only guard generated by servePool.checked, not an
// arbitrary REPL call or a title-based allowlist. No lost side effect is replayed.
const code=`{const o=globalThis.parkedResident;
if(nodeRepl.requestMeta?.threadId!==${J(expected.parent)}||o?.attempt!==${J(expected.owner)}||
o.socket!==globalThis.parkedSocket||JSON.stringify(o.socket.config)!==o.descriptor)
throw Error("Resident owner changed");if(o.binding!==globalThis.parkedBinding||nodeRepl.requestMeta?.["x-codex-turn-metadata"]?.turn_id!==o.binding.turn)throw Error("Native turn changed");console.log("{}");}`;
// Older relays lack per-call dispatch metadata. They qualify only if every
// pending call is still in the retained, never-emitted queue. Missing metadata
// for an already-emitted call is uncertainty, not permission to reconstruct it.
const calls=[...r.pending].map(([id,entry])=>({entry,request:entry.request??
(r.calls.filter(c=>c.id===id).length===1?r.calls.find(c=>c.id===id):null)}));
if(calls.some(c=>c.request?.tool!=="mcp__node_repl__js"||
J(c.request.args)!==J({code,timeout_ms:60000,title:"Resident pool owner"})))
throw Error("Outstanding call is not a retained owner check; collect-only recovery required");
// Fence the old outer driver synchronously, including replies arriving while
// canonical checks are in progress. Failure leaves this fence in place.
r.continuing=token;
const current=(await cli(["resident","check",J(o.credentials)])).owner;
if(!current||!["generation","owner","parent","worker_pool_sha256"].every(k=>current[k]===expected[k])||
J(current.session)!==J(expected.session))throw Error("Canonical continuation owner changed");
const slots=current.slots?.filter(s=>s.request!==null);
if(!slots||slots.length!==calls.length||slots.some(s=>
s.invocation?.request!==s.request||
!s.invocation?.invocation?.startsWith(o.serveInvocation+"-")||s.phase==="collect_only"))
throw Error("Retained pool invocation differs; collect-only recovery required");
for(const s of slots){
const a=(await cli(["status",s.request])).assignment;
if(a?.assignment_id!==s.request||a.parent_task_id!==expected.parent||
a.worker_conversation_id!==s.worker_conversation_id||a.status!=="armed"||
a.no_resend!==true||a.submission_count!==0)
throw Error("Retained armed request differs; collect-only recovery required");
}
if(J((await cli(["resident","check",J(o.credentials)])).owner)!==J(current))
throw Error("Canonical continuation state changed");
if(g.parkedResident!==o||o.binding!==binding||g.parkedBinding!==binding||
g.parkedSocket!==r.socket||r.continuing!==token||
meta?.threadId!==expected.parent||meta?.["x-codex-turn-metadata"]?.turn_id!==turn||
calls.some(c=>r.pending.get(c.request.id)!==c.entry))throw Error("Continuation proof changed");
// Rebind the same promises, invocation and request identities. Never run serve
// or rendezvous again. Only the interrupted read-only guards are reissued.
o.binding=g.parkedBinding=r.binding=Object.freeze({broker:expected.parent,turn});
o.serveExistingClaim=r.token=token;
r.retiredTurns??=new Set();r.retiredTurns.add(binding.turn);
r.calls=calls.map(c=>{c.entry.request=c.request;c.entry.dispatched=false;return c.request;});
r.resumePoll=true;delete r.continuing;
return {claimed:true,continued:true};
}

export async function serveExistingStep(g,meta,token,reply){
const r=retainedRelay(g,meta,token),o=g.parkedResident;
if(!r.started){
if(reply!==null)throw Error("Unexpected relay reply");
r.started=true;r.pending=new Map();r.calls=[];r.outputs=[];r.serial=0;r.done=false;
const host=Object.fromEntries(["exec_command","write_stdin","mcp__node_repl__js","mcp__codex_app__read_thread","mcp__codex_app__send_message_to_thread","mcp__codex_app__navigate_to_codex_page"].map(tool=>[tool,args=>new Promise((resolve,reject)=>{
const id=++r.serial,request={id,tool,args};r.pending.set(id,{resolve,reject,request,dispatched:false});r.calls.push(request);
})]));
const text=value=>r.outputs.push(value),trusted=JSON.parse(o.descriptor);
const serve=r.expected.worker_pool_sha256?servePool:serveResident;
r.running=(async()=>{
await serve(host,text,trusted,r.expected.parent,r.expected.owner,residentLifecycle,token,poolRecoveryPlan,recoverPoolRequests);
const joined=await host.mcp__node_repl__js({code:`console.log(JSON.stringify(await globalThis.parkedResident.activation.recordResidentJoined(globalThis,nodeRepl.requestMeta,${J(r.expected.owner)})));`,timeout_ms:60000,title:"Record joined resident execution"});
if(joined?.isError===true)throw Error("Native join barrier failed");
text(joined);
})().then(()=>{r.done=true;},e=>{r.error=String(e?.message||e);});
}else if(r.resumePoll&&reply===null){
r.resumePoll=false;
}else{
if(!reply||!r.pending.get(reply.id)?.dispatched||!Object.hasOwn(reply,"value")&&!Object.hasOwn(reply,"error"))
throw Error("Unknown or consumed relay reply; preserve claim");
const call=r.pending.get(reply.id);r.pending.delete(reply.id);
if(Object.hasOwn(reply,"error"))call.reject(Error(reply.error));else call.resolve(reply.value);
}
// Settle the continuation's microtasks, never wait on a host call inside REPL.
await new Promise(resolve=>setImmediate(resolve));
// Upgrade only calls still in the original closure's un-emitted queue.
for(const request of r.calls){
const call=r.pending.get(request.id);
if(!call)throw Error("Unbound retained relay call");
if(call.request===undefined){call.request=request;call.dispatched=false;}
}
return {calls:r.calls.splice(0),outputs:r.outputs.splice(0),done:r.done,error:r.error??null};
}

async function serveExistingPacket(directory,replaceUnused=false,continuation=false){
await privateDirectory(directory);
const current=await cli(["resident","inspect"]),owner=current.owner;
if(!owner?.session||owner.session.directory!==directory)
throw Error("Bound resident session required");
const raw=await privateBytes(directory+"/session.json",16384),trusted=JSON.parse(raw);
if(createHash("sha256").update(raw).digest("hex")!==owner.session.descriptor_sha256||
trusted.helper!==helper||trusted.parent!==owner.parent||trusted.resident!==true)
throw Error("Bound resident descriptor differs");
const expected={generation:owner.generation,owner:owner.owner,parent:owner.parent,
...(owner.worker_pool_sha256?{worker_pool_sha256:owner.worker_pool_sha256}:{worker:owner.worker}),
session:owner.session};
const token=randomBytes(16).toString("hex");
const path=fileURLToPath(import.meta.url),hash=createHash("sha256").update(await fs.readFile(path)).digest("hex");
const code=`{try{
const fs=await import("node:fs/promises"),crypto=await import("node:crypto");
if(await fs.realpath(${J(path)})!==${J(path)}||crypto.createHash("sha256").update(await fs.readFile(${J(path)})).digest("hex")!==${J(hash)})throw Error("Activation pin changed");
const activation=await import(${J(pathToFileURL(path).href+"?sha256="+hash)});
console.log(JSON.stringify(await activation.${replaceUnused?"replaceUnusedServing":continuation?"continueServeExisting":"claimServeExisting"}(globalThis,nodeRepl.requestMeta,${J(expected)},${J(token)})));
}catch(e){console.log(JSON.stringify({claimed:false,error:String(e?.message||e)}));}}`;
if(replaceUnused)return {kind:"native_unused_replacement_packet",session:owner.session,generation:owner.generation,calls:{replace:`
const raw=await tools.mcp__node_repl__js(${J({code,timeout_ms:60000,title:"Replace consumed unused serving session"})});
if(raw?.isError===true||raw?.status==="failed"||raw?.content?.length!==1||raw.content[0].type!=="text")throw Error("Unused replacement uncertain; preserve evidence");
const value=JSON.parse(raw.content[0].text);
if(value?.replaced!==true)throw Error("Unused replacement rejected or uncertain: "+(value?.error||"missing receipt"));
text(value);`}};
const relayCode=`{try{const a=await import(${J(pathToFileURL(path).href+"?sha256="+hash)});console.log(JSON.stringify(await a.serveExistingStep(globalThis,nodeRepl.requestMeta,${J(token)},REPLY)));}catch(e){console.log(JSON.stringify({calls:[],outputs:[],done:false,error:String(e?.message||e)}));}}`;
const dispatchCode=`{const a=await import(${J(pathToFileURL(path).href+"?sha256="+hash)});console.log(JSON.stringify(a.claimRelayCall(globalThis,nodeRepl.requestMeta,${J(token)},CALL_ID)));}`;
const serve=`// @exec: {"yield_time_ms":1000}
let claim;
try{claim=await tools.mcp__node_repl__js(${J({code,timeout_ms:60000,title:"Guarded serve-existing claim"})});}
catch{throw Error("Serve-existing claim uncertain: host call failed; preserve session");}
if(claim?.isError===true||claim?.status==="failed"||claim?.content?.length!==1||claim.content[0].type!=="text")throw Error("Serve-existing claim uncertain: missing or failed host result; preserve session");
let receipt;
try{receipt=JSON.parse(claim.content[0].text);}catch{throw Error("Serve-existing claim uncertain: invalid host result; preserve session");}
if(receipt?.claimed!==true)throw Error("Serve-existing claim rejected or uncertain; preserve session: "+(typeof receipt?.error==="string"?receipt.error:"missing claim receipt"));
const pending=new Map(),allowed=new Set(["exec_command","write_stdin","mcp__node_repl__js","mcp__codex_app__read_thread","mcp__codex_app__send_message_to_thread","mcp__codex_app__navigate_to_codex_page"]);
let reply=null;
for(;;){
const raw=await tools.mcp__node_repl__js({code:${J(relayCode)}.replace("REPLY",()=>JSON.stringify(reply)),timeout_ms:60000,title:"Serve-existing relay"});
if(raw?.isError===true||raw?.status==="failed"||raw?.content?.length!==1||raw.content[0].type!=="text")throw Error("Serve-existing relay uncertain; preserve session");
const step=JSON.parse(raw.content[0].text);
for(const output of step.outputs)text(output);
if(step.error)throw Error(step.error);
if(step.done){if(pending.size)throw Error("Unjoined relay calls");break;}
for(const call of step.calls){
if(!allowed.has(call.tool)||pending.has(call.id))throw Error("Invalid relay call");
pending.set(call.id,(async()=>{try{
const claimed=await tools.mcp__node_repl__js({code:${J(dispatchCode)}.replace("CALL_ID",()=>JSON.stringify(call.id)),timeout_ms:60000,title:"Consume retained relay call"});
if(claimed?.isError===true||claimed?.status==="failed"||claimed?.content?.length!==1||claimed.content[0].type!=="text")throw Error("Relay dispatch uncertain; preserve call");
const bound=JSON.parse(claimed.content[0].text);
if(JSON.stringify(bound)!==JSON.stringify(call))throw Error("Relay dispatch identity differs");
return {id:call.id,value:await tools[call.tool](call.args)};
}catch(e){return {id:call.id,error:String(e?.message||e)};}})());
}
if(!pending.size)throw Error("Serve-existing relay stalled; preserve session");
reply=await Promise.race(pending.values());pending.delete(reply.id);
}`;
return {kind:continuation?"native_post_arm_continuation_packet":"native_serve_existing_packet",lifecycle:residentLifecycle,
session:owner.session,generation:owner.generation,calls:{serve}};
}

function buildPoolServe(trusted,parent,attempt){
const body=servePool.toString().replace("createRunner(tools,text)","("+createRunner.toString()+")(tools,text)");
return joinedServe(`const poolRecoveryPlan=${poolRecoveryPlan.toString()},recoverPoolRequests=${recoverPoolRequests.toString()};\nawait (${body})(tools,text,${J(trusted)},${J(parent)},${J(attempt)},${J(residentLifecycle)},undefined,poolRecoveryPlan,recoverPoolRequests);`,attempt);
}

async function poolResidentPacket(broker,parent,workers,root){
if(broker!==parent||!validId(broker)||!Array.isArray(workers)||
workers.length<1||workers.length>2||
workers.some(worker=>!validId(typeof worker==="string"?worker:worker?.conversation_id)))
throw Error("One or two trusted worker IDs required");
if(root===undefined)throw Error("Private client root required");
await privateDirectory(root);
const s=await cli(["status","--current"]),ownership=await cli(["resident","inspect"]);
const configured=s.worker_pool?.workers;
if(!Array.isArray(configured)||configured.length!==workers.length||
configured.some((entry,index)=>entry.conversation_id!==
(typeof workers[index]==="string"?workers[index]:workers[index].conversation_id)))
throw Error("Production worker pool mismatch");
if(!configured.every(entry=>["user-confirmed-worker","user-confirmed-pro"].includes(entry.model_confirmation)))
throw Error("Production worker confirmation mismatch");
const trusted={helper,configDir:s.paths.config_dir,stateDir:s.paths.state_dir,parent,
workers:configured.map(entry=>({slot:entry.slot,conversation_id:entry.conversation_id})),
worker_pool_sha256:s.worker_pool.file_sha256,workerPoolSha256:s.worker_pool.file_sha256,
resident:true,maxConcurrentRequests:configured.length,
leaseMs:null,idleMs:45000,replyMs:3900000,maxSnapshots:6,observationMs:50000,activeJobMs:3600000};
const attempt=randomBytes(16).toString("hex"),expected={generation:ownership.owner?.generation??0,
worker_pool_sha256:trusted.worker_pool_sha256};
const path=fileURLToPath(import.meta.url),hash=createHash("sha256").update(await fs.readFile(path)).digest("hex");
const open=`{
const fs=await import("node:fs/promises"),crypto=await import("node:crypto");
if(await fs.realpath(${J(path)})!==${J(path)}||crypto.createHash("sha256").update(await fs.readFile(${J(path)})).digest("hex")!==${J(hash)})throw Error("Activation pin changed");
const activation=await import(${J(pathToFileURL(path).href+"?sha256="+hash)});
console.log(JSON.stringify(await activation.openResident(globalThis,nodeRepl.requestMeta,${J(trusted)},${J(expected)},${J(attempt)},${J(root)})));
}`;
return {kind:"native_activation_packet",authorization:"required_separately",broker,parent,workers:configured,
trusted,pins,openAttempt:attempt,lifecycle:residentLifecycle,calls:{open:"text(await tools.mcp__node_repl__js("+J({code:open,timeout_ms:60000,title:"Resident pool open"})+"));",
serve:buildPoolServe(trusted,parent,attempt)}};
}

async function residentPacket(broker,parent,worker,root){
if(Array.isArray(worker))return await poolResidentPacket(broker,parent,worker,root);
if(broker!==parent||![broker,parent,worker].every(validId))throw Error("Trusted owner IDs required");
if(root===undefined)throw Error("Private client root required");
await privateDirectory(root);
const s=await cli(["status","--current"]),ownership=await cli(["resident","inspect"]);
if(Array.isArray(s.worker_pool?.workers)&&s.worker_pool.workers.length)
throw Error("Worker pool is active; use the pool resident packet");
if(s.worker?.conversation_id!==worker||
!["user-confirmed-worker","user-confirmed-pro"].includes(s.worker?.model_confirmation))
throw Error("Production worker mismatch");
const trusted={helper,configDir:s.paths.config_dir,stateDir:s.paths.state_dir,parent,worker,
resident:true,leaseMs:null,idleMs:45000,replyMs:3900000,maxSnapshots:6,
observationMs:50000,activeJobMs:3600000};
const attempt=randomBytes(16).toString("hex"),expected={generation:ownership.owner?.generation??0};
const path=fileURLToPath(import.meta.url),hash=createHash("sha256").update(await fs.readFile(path)).digest("hex");
const open=`{
const fs=await import("node:fs/promises"),crypto=await import("node:crypto");
if(await fs.realpath(${J(path)})!==${J(path)}||
crypto.createHash("sha256").update(await fs.readFile(${J(path)})).digest("hex")!==${J(hash)})throw Error("Activation pin changed");
const activation=await import(${J(pathToFileURL(path).href+"?sha256="+hash)});
console.log(JSON.stringify(await activation.openResident(globalThis,nodeRepl.requestMeta,
${J(trusted)},${J(expected)},${J(attempt)},${J(root)})));
}`;
return {kind:"native_activation_packet",authorization:"required_separately",broker,parent,worker,trusted,pins,
openAttempt:attempt,lifecycle:residentLifecycle,calls:{open:"text(await tools.mcp__node_repl__js("+J({code:open,timeout_ms:60000,title:"Resident open"})+"));",serve:buildResidentServe(trusted,parent,attempt)}};
}

function buildResidentServe(trusted,parent,attempt){
const body=serveResident.toString().replace("createRunner(tools,text)","("+createRunner.toString()+")(tools,text)");
return joinedServe(`const poolRecoveryPlan=${poolRecoveryPlan.toString()},recoverPoolRequests=${recoverPoolRequests.toString()};\nawait (${body})(tools,text,${J(trusted)},${J(parent)},${J(attempt)},${J(residentLifecycle)},undefined,poolRecoveryPlan,recoverPoolRequests);`,attempt);
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

async function packet(broker,parent,worker,closedDirectory,resume,terminal){
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
!["user-confirmed-worker","user-confirmed-pro"].includes(s.worker?.model_confirmation))
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
${terminal===undefined?"":`const {execFile}=await import("node:child_process");
const helper=${J(helper)};
${cli.toString()}
${absent.toString()}
${terminalCheck.toString()}`}
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
if(previous.queuedResume||previous.terminal){
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
const tmp=await fs.mkdtemp(nodeRepl.tmpDir.replace(/\\/$/,"")+"/pro-session-");
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
receive:header+"text("+call(receive)+");\n",dispatch
}};
}
async function ready(directory,ordinal){
if(!Number.isInteger(ordinal)||ordinal<1||ordinal>64)
throw Error("Invalid ready ordinal");
if((await fs.lstat(directory)).uid!==process.getuid()) throw Error("Wrong owner");
const {loadSession}=await import("./parked-socket.mjs");
const c=await loadSession(directory);
return await watchFile(directory,25000,"Readiness not observed",async()=>{
if(c.resident!==true&&Date.now()>=c.expiresAt) throw Error("Session expired");
await absent(directory+"/transport-audit.json");
const v=JSON.parse(await fs.readFile(join(directory,"ready-"+ordinal+".json"),"utf8"));
if(v.sessionId!==c.sessionId||v.ordinal!==ordinal) throw Error("Wrong readiness");
return {ready:true,sessionId:c.sessionId,ordinal,
meaning:"not an admission guarantee"};
}).promise;
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

function workerId(worker){
return typeof worker==="string"?worker:worker?.conversation_id;
}
function authorityWorkersMatch(s,c){
if(Array.isArray(c.workers)&&c.workers.length){
const configured=s.worker_pool?.workers||[];
return configured.length===c.workers.length&&
c.workers.every((worker,index)=>configured[index]?.conversation_id===workerId(worker))&&
configured.every(entry=>["user-confirmed-worker","user-confirmed-pro"].includes(entry.model_confirmation));
}
return s.worker?.conversation_id===c.worker&&
["user-confirmed-worker","user-confirmed-pro"].includes(s.worker?.model_confirmation);
}
function authorityOccupied(s,q,c){
if(s.active_cooldown!==null)return true;
const capacity=Array.isArray(c.workers)&&c.workers.length?c.workers.length:1;
const active=Array.isArray(s.active_assignments)?s.active_assignments.length:
(s.active_assignment?1:0);
const claimed=Array.isArray(q.requests)?q.requests.filter(r=>r.state==="claimed").length:0;
return active>=capacity||claimed>=capacity;
}

async function rendezvousSession(directory,ordinal,requestId){
if(!Number.isInteger(ordinal)||ordinal<1||ordinal>64||!validId(requestId))
throw Error("Invalid rendezvous identity");
if((await fs.lstat(directory)).uid!==ownerUid)throw Error("Wrong session owner");
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

export function watchFile(directory,ms,timeoutMessage,inspect,watchDirectory=watch){
let cancel,retryTimer;
const promise=new Promise((resolve,reject)=>{
let done=false,busy=false,again=false,deferred=false;
const w=watchDirectory(directory,()=>{again=true;deferred=false;void check();});
const timer=setTimeout(()=>end(Error(timeoutMessage)),ms);
function end(error,value){
if(done)return;done=true;clearTimeout(timer);clearTimeout(retryTimer);w.close();
error?reject(error):resolve(value);
}
cancel=()=>end(Error("Rendezvous stopped"));
async function check(){
if(done||busy)return;
busy=true;
const eventDriven=again;
again=false;
let retry=false;
try{
const value=await inspect();
if(value!==undefined)end(null,value);
}catch(e){
// Native ready files are written directly. Recheck a coalesced create/write
// from the same event (and at most one deferred follow-up). Never start a
// timer polling loop, and never treat absence as unsent.
if(e.code!=="ENOENT"&&e.code!=="EAGAIN"&&!(e instanceof SyntaxError))end(e);
else retry=e.code==="EAGAIN"||e instanceof SyntaxError||eventDriven;
}finally{
busy=false;
if(done)return;
if(again){deferred=false;void check();return;}
if(retry&&!deferred){
deferred=true;
retryTimer=setTimeout(()=>{retryTimer=undefined;void check();},25);
}
}
}
w.on("error",e=>end(e));void check();
});
promise.catch(()=>{});
return {promise,cancel:()=>cancel?.()};
}

function waitRecord(directory,name,validate,ms){
return watchFile(directory,ms,"Rendezvous deadline; do not retry",async()=>{
await absent(directory+"/transport-audit.json");
const value=JSON.parse((await privateBytes(directory+"/"+name,4096)).toString("utf8"));
validate(value);
return value;
});
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
const ownership=c.resident===true?await requireResidentOwnership(c,directory):null;
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
!authorityWorkersMatch(s,c))throw Error("Rendezvous authority mismatch");
if(authorityOccupied(s,q,c)||!Array.isArray(q.requests))
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
prior.parent_task_id!==c.parent||
!(Array.isArray(c.workers)?c.workers.some(w=>workerId(w)===prior.worker_conversation_id):
prior.worker_conversation_id===c.worker))
throw Error("Complete queued recovery before another request");
}
await absent(c.stateDir+"/native-client");
await absent(directory+"/ready-"+ordinal+".json");
await absent(directory+"/command-observed-"+ordinal+".json");
const name="command-"+ordinal+(attempt===undefined?"":"-retry-"+attempt);
await absent(directory+"/"+name+".json");
const ticket=directory+"/"+name; // Exclusive attempt marker; never remove to retry.
const snapshot=ticket+"/prompt.txt";
if(c.resident!==true){
await fs.mkdir(ticket,{mode:448});
if(!resume){
await fs.writeFile(snapshot,raw,{flag:"wx",mode:384});
await privateBytes(snapshot,4194304);
}
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
if(ownership){
const {generation,owner,parent,worker,worker_pool_sha256,session}=ownership.owner;
await cli(["resident","admit",J({generation,owner,parent,worker,worker_pool_sha256,session,
command:J(record),prompt_file:promptFile,retry:attempt!==undefined})]);
}else{
await fs.writeFile(ticket+"/ready.tmp",J(record),{flag:"wx",mode:384});
await fs.rename(ticket+"/ready.tmp",directory+"/"+name+".json");
}
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
// waiting-N.<sessionId>, whose mtime the native admission wait keeps fresh.
// A rendezvous claims it by renaming it into its ticket; the waiter retires
// it with rmdir. Both are atomic, so exactly one side wins: a retired waiter
// leaves nothing to claim (no ticket exists), and a waiter that loses the
// race sees ENOENT and stays responsible for observing that publication. A
// killed waiter cannot retire; its marker goes stale within WAITER_FRESH_MS.
const WAITER_FRESH_MS=5000;
const waiterPath=(directory,ordinal,sessionId)=>directory+"/waiting-"+ordinal+"."+sessionId;
const freshWaiter=(stat,now=Date.now())=>stat.isDirectory()&&stat.uid===ownerUid&&
(stat.mode&511)===448&&now-stat.mtimeMs>=-1000&&now-stat.mtimeMs<=WAITER_FRESH_MS;

async function requireResidentOwnership(c,directory){
const ownership=await residentOwnership(c,directory);
if(ownership.reason!=="current_owner")throw Error("Resident unavailable: "+ownership.reason+"; preserve session");
return ownership;
}

async function residentOwnership(c,directory){
if(c.helper!==helper)return {reason:"installed_helper_mismatch"};
const s=await cli(["status","--current"]),v=await cli(["resident","inspect"]);
const poolMode=Array.isArray(c.workers);
if(s.paths?.config_dir!==c.configDir||s.paths?.state_dir!==c.stateDir||
(poolMode?(s.worker_pool?.file_sha256!==c.worker_pool_sha256||
!Array.isArray(s.worker_pool?.workers)||s.worker_pool.workers.length!==c.workers.length):
s.worker?.conversation_id!==c.worker)) return {reason:"canonical_authority_mismatch"};
const bound=v.owner?.session;
if(!bound)return {reason:"unbound_owner"};
if(bound.directory!==directory||bound.session_id!==c.sessionId||
v.owner.parent!==c.parent||(poolMode?v.owner.worker_pool_sha256!==c.worker_pool_sha256:
v.owner.worker!==c.worker))
return {reason:"retired_owner"};
const raw=await privateBytes(directory+"/session.json",16384);
if(createHash("sha256").update(raw).digest("hex")!==bound.descriptor_sha256||
J(JSON.parse(raw.toString("utf8")))!==J(c))return {reason:"descriptor_changed"};
return {reason:"current_owner",owner:v.owner,status:s};
}

// A read-only snapshot, never a liveness guarantee or takeover permission.
// Check explicit owner-provided paths. Do not discover authority by glob/mtime.
async function residentStatus(directory){
const base={kind:"resident_status",version:1,directory,maxConcurrentRequests:1,
admissionObserved:false,sendAuthorized:false,replacementAuthorized:false};
try{
await privateDirectory(directory);
const {loadSession}=await import("./parked-socket.mjs");
  const c=await loadSession(directory);
  if(c.resident!==true||!/^[a-f0-9]{32}$/.test(c.sessionId))throw Error("Not a resident session");
  base.maxConcurrentRequests=c.maxConcurrentRequests||(Array.isArray(c.workers)?c.workers.length:1);
  if(Array.isArray(c.workers)&&c.workers.length)base.workers=c.workers;
base.sessionId=c.sessionId;
const owned=await residentOwnership(c,directory);
if(owned.reason!=="current_owner")return {...base,state:"unavailable",reason:owned.reason};
base.generation=owned.owner.generation;
const names=await fs.readdir(directory);
if(names.includes("transport-audit.json")){
const audit=JSON.parse((await privateBytes(directory+"/transport-audit.json",1048576)).toString("utf8"));
if(audit.sessionId!==c.sessionId||typeof audit.reason!=="string"||!Array.isArray(audit.events)||
names.includes("wake.sock"))throw Error("Conflicting closure evidence");
return {...base,state:"closed_audited",reason:"matching_closure_audit"};
}
if(names.some(n=>["resident-stop","resident-stop.json","resident-failure.json","resident-failure-final.json"].includes(n)))
return {...base,state:"unavailable",reason:"stop_or_failure_evidence"};
const occupied=Array.isArray(base.workers)&&base.workers.length
  ?(owned.owner.slots||[]).filter(slot=>slot.request!==null).length
  :(owned.owner.inflight!==null||owned.status.active_assignment!==null?1:0);
if(occupied>=base.maxConcurrentRequests)
return {...base,state:"busy",reason:"request_or_invocation_reserved"};
if(owned.status.active_cooldown!==null)return {...base,state:"unavailable",reason:"account_cooldown"};
const markers=names.filter(n=>/^waiting-/.test(n));
if(markers.length===0||!names.includes("wake.sock"))
return {...base,state:"not_waiting",reason:"no_admission_observed"};
const match=/^waiting-([1-9][0-9]*)\.([a-f0-9]{32})$/.exec(markers[0]);
if(markers.length!==1||!match||Number(match[1])>64||match[2]!==c.sessionId)
throw Error("Ambiguous resident readiness");
const ordinal=Number(match[1]),marker=directory+"/"+markers[0];
if(!(await fs.lstat(directory+"/wake.sock")).isSocket())throw Error("Invalid socket evidence");
if(!freshWaiter(await fs.lstat(marker)))return {...base,state:"stale_readiness",reason:"waiter_not_fresh"};
if((await fs.readdir(marker)).length||names.some(n=>
n==="command-"+ordinal||n==="ready-"+ordinal+".json"||n==="command-observed-"+ordinal+".json"||
new RegExp("^command-"+ordinal+"(?:-retry-[a-f0-9]{32})?[.]json$").test(n)))
throw Error("Consumed or conflicting ordinal evidence");
// Recheck canonical generation and waiter after observation. Final admission
// still belongs to rendezvous' atomic claim and the helper's locked guards.
const after=await residentOwnership(c,directory);
const laterOccupied=Array.isArray(base.workers)&&base.workers.length
  ?(after.owner.slots||[]).filter(slot=>slot.request!==null).length
  :(after.owner.inflight!==null||after.status.active_assignment!==null?1:0);
if(after.reason!=="current_owner"||laterOccupied>=base.maxConcurrentRequests||
after.status.active_cooldown!==null||!freshWaiter(await fs.lstat(marker)))
return {...base,state:"not_waiting",reason:"snapshot_changed"};
return {...base,state:"admission_observed",reason:"current_owner_waiting",ordinal,admissionObserved:true};
}catch(e){
return {...base,state:e.code==="ENOENT"?"not_waiting":"malformed_preserve",
reason:e.code==="ENOENT"?"evidence_missing_or_changed":"evidence_unverifiable"};
}
}

async function residentNext(directory,ordinal,signal,observationMs=25000){
const c=await rendezvousSession(directory,ordinal,"resident-next");
if(c.resident!==true||c.helper!==helper)throw Error("Resident mismatch");
await absent(directory+"/ready-"+ordinal+".json");
await absent(directory+"/command-observed-"+ordinal+".json");
const marker=waiterPath(directory,ordinal,c.sessionId);
await fs.mkdir(marker,{mode:448});
const beat=setInterval(()=>fs.utimes(marker,new Date(),new Date()).catch(()=>{}),1000);
let claimed=false,choice,withdrawal;
async function retire(){
if(!claimed)try{await fs.rmdir(marker);}catch(e){if(e.code!=="ENOENT")throw e;claimed=true;}
return claimed;
}
try{
choice=await new Promise((resolve,reject)=>{
let done=false,busy=false,again=false,deferred=false,bound,elapsed=false,outcome;
const observation=setTimeout(()=>{elapsed=true;again=true;void check();},observationMs);
const abort=()=>{
clearInterval(beat);
withdrawal=retire();withdrawal.catch(()=>{});
end(signal.reason);
};
const settle=()=>{if(done&&!busy){const [e,v]=outcome;e?reject(e):resolve(v);}};
function end(e,v){if(done)return;done=true;clearTimeout(bound);clearTimeout(observation);
signal?.removeEventListener("abort",abort);w.close();outcome=[e,v];settle();}
const w=watch(directory,()=>{again=true;void check();});
w.on("error",e=>end(e));
signal?.addEventListener("abort",abort,{once:true});
async function check(){
if(done||busy)return;busy=true;again=false;
let torn=false;
try{
signal?.throwIfAborted();
await absent(directory+"/transport-audit.json");
try{
const stop=JSON.parse((await privateBytes(directory+"/resident-stop.json",4096)).toString("utf8"));
if(Object.keys(stop).join(",")!=="sessionId"||stop.sessionId!==c.sessionId)
throw Error("Invalid stop");
// A claim that beat this stop holds the waiter here until its command appears.
if(!await retire()){end(null,{stopped:true});return;}
}catch(e){if(e.code!=="ENOENT")throw e;}
// A bounded native observation returns idle only if retirement beat the
// client's atomic claim. A winning claimant keeps the publication bound.
if(elapsed&&!await retire()){end(null,{pending:true});return;}
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
}catch(e){
if((e.code==="EAGAIN"||e instanceof SyntaxError)&&!deferred)torn=true;
else end(e);
}finally{
busy=false;settle();
if(done)return;
if(again){deferred=false;void check();return;}
if(torn){deferred=true;setImmediate(()=>void check());}
}
}
if(signal?.aborted)abort();else void check();
});
}finally{clearInterval(beat);await withdrawal;await retire();}
signal?.throwIfAborted();
if(choice.stopped||choice.pending)return {...choice,sessionId:c.sessionId};
const s=await cli(["status","--current"],10000),q=await cli(["queue","status"],10000);
// Same requestId in the queue is a duplicate rendezvous. Pool occupancy
// already allows sibling queued work; native submit happens after ready-N.
if(s.paths?.state_dir!==c.stateDir||s.paths?.config_dir!==c.configDir||
!authorityWorkersMatch(s,c)||authorityOccupied(s,q,c)||
!Array.isArray(q.requests)||q.requests.some(r=>r.request_id===choice.v.requestId))
throw Error("Authority changed");
await absent(c.stateDir+"/native-client");
signal?.throwIfAborted();
return {sessionId:c.sessionId,command:choice.v,attempt:choice.attempt};
}

// One native writer and one awaited owner. The detector is renewed by outer
// calls, never by a background heartbeat. It only covers admission, not Pro.
export async function residentAdmission(g,meta,invocation,ordinal,next,workActive=false){
const o=g.parkedResident;
if(!o||!o.used||o.serveInvocation!==invocation||o.socket!==g.parkedSocket||
o.binding!==g.parkedBinding||JSON.stringify(o.socket.config)!==o.descriptor||
meta?.threadId!==o.binding.broker||meta?.["x-codex-turn-metadata"]?.turn_id!==o.binding.turn)
throw Error("Wrong resident admission owner");
if(o.credentials){
const current=await cli(["resident","check",J(o.credentials)]);
if(next!==undefined){
const slots=current.owner.slots;
if(Array.isArray(slots)){
if(!slots.some(slot=>slot.request===next.command?.requestId&&slot.invocation))
throw Error("Canonical invocation must reserve admission first");
}else if(current.owner.inflight?.invocation!==invocation||
current.owner.inflight?.request!==next.command?.requestId)
throw Error("Canonical invocation must reserve admission first");
}
}
const a=o.admission??={expired:false,timer:null,work:null,controller:null,deadlineAt:null};
if(a.deadlineAt!==null&&Date.now()>=a.deadlineAt)a.expire();
if(a.expired||a.work)throw Error("Resident admission ended or occupied");
const controller=new AbortController();a.controller=controller;
a.expire=()=>{
if(a.expired)return;
a.expired=true;clearTimeout(a.timer);
const error=Error("Resident serving evaluation stopped renewing admission");
controller.abort(error);
// Close synchronously withdraws socket admission and releases receive(), so
// joining an in-flight receive cannot deadlock failure finalization.
const closed=o.socket.close("resident_failed");closed.catch(()=>{});
a.failure=(async()=>{
// Joining prevents closure proof from racing a still-running marker writer.
await a.work?.catch(()=>{});
let stopRequested=false;
try{
const stop=JSON.parse((await privateBytes(o.directory+"/resident-stop.json",4096)).toString("utf8"));
stopRequested=Object.keys(stop).join(",")==="sessionId"&&stop.sessionId===o.socket.config.sessionId;
}catch(e){if(e.code!=="ENOENT")throw e;}
try{await recordResidentFailure(o,{sessionId:o.socket.config.sessionId,at:Date.now(),
reason:"resident_failed",stopRequested,pendingHelperSession:null,
requestId:g.parkedDelivery?.requestId??null,heldDelivery:g.parkedDelivery,
error:{name:error.name,message:error.message,stack:error.stack}});}
finally{await closed;}
})();
a.failure.catch(()=>{}); // Preserved and joined by owner cleanup, never retried.
};
clearTimeout(a.timer);a.deadlineAt=Date.now()+60000;
a.timer=setTimeout(a.expire,60000);
a.work=(async()=>{
// The native REPL serializes calls. Do not hold it for an idle 25-second
// observation while a sibling needs it to send, save evidence or publish.
// Only the outer serving loop chooses this bounded observation; it neither
// renews admission in the background nor changes the command publication bound.
if(next===undefined)return await residentNext(o.directory,ordinal,controller.signal,workActive?250:25000);
if(next.sessionId!==o.socket.config.sessionId||next.command?.ordinal!==ordinal)
throw Error("Resident command mismatch");
// The selected immutable command must still exist. Never start another long
// command waiter inside this native call if its evidence has disappeared.
await privateBytes(o.directory+"/command-"+ordinal+(next.attempt?"-retry-"+next.attempt:"")+".json",4096);
const ready=await commandReady(o.directory,ordinal,next.command.requestId,next.attempt);
controller.signal.throwIfAborted();
if(JSON.stringify(ready)!==JSON.stringify({commandReady:true,...next.command,meaning:ready.meaning}))
throw Error("Command changed");
await absent(o.directory+"/resident-stop.json");
if(g.parkedDelivery!==null||Date.now()>=ready.deadlineAt)throw Error("Delivery held or gate expired");
g.parkedDelivery=await o.socket.receive();
controller.signal.throwIfAborted();
return g.parkedDelivery;
})();
try{
const result=await a.work;
controller.signal.throwIfAborted();
if(o.credentials)await cli(["resident","check",J(o.credentials)]);
// Pro uses its own budgets. A stop still needs owner cleanup, so retain its
// detector until cleanup joins us and closes the socket.
if(next!==undefined){clearTimeout(a.timer);a.deadlineAt=null;}
return result;
}finally{a.work=null;a.controller=null;}
}

export async function stopResidentAdmission(o){
const a=o.admission;if(!a)return;
clearTimeout(a.timer);a.deadlineAt=null;
// A lost tool response may leave its native receive alive. Withdraw that
// admission before joining it, exactly as the owner-loss detector does.
let closed;
if(a.work){a.expired=true;closed=o.socket.close("resident_failed");closed.catch(()=>{});}
a.controller?.abort(Error("Resident admission stopped"));
await a.work?.catch(()=>{});
await closed;
await a.failure;
}

export async function recordResidentFailure(o,summary,final=false){
// Preserve the first failure and the final operation identities independently.
if(final)await recordResidentFailure(o,summary).catch(()=>{summary={...summary,firstRecordFailed:true};});
const key=final?"failureFinalRecord":"failureRecord",name=final?"resident-failure-final.json":"resident-failure.json";
return await(o[key]??=(async()=>{
const h=await fs.open(o.directory+"/"+name,"wx",384);
try{await h.writeFile(J(summary));await h.sync();}finally{await h.close();}
const d=await fs.open(o.directory,"r");try{await d.sync();}finally{await d.close();}
})());
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
result=await residentPacket(args[0],args[1],
  (args[2].startsWith("[")?JSON.parse(args[2]):args[2]),args[3]);
else if(action==="resident-pool-packet"&&args.length===4)
result=await poolResidentPacket(args[0],args[1],JSON.parse(args[2]),args[3]);
else if(action==="closed-packet"&&args.length===4) result=await packet(...args);
else if(action==="client-preflight"&&args.length===1)
result=await clientPreflight(args[0]);
else if(action==="closed-queued-packet"&&args.length===11)
result=await packet(...args.slice(0,4),{
sessionId:args[4],requestId:args[5],fingerprint:args[6],callId:args[7],
clientSessionId:args[8],nonce:args[9],promptSha256:args[10]
});
else if(action==="closed-terminal-packet"&&[16,19].includes(args.length))
result=await packet(...args.slice(0,4),undefined,{
oldHelper:args[4],oldWorker:args[5],sessionId:args[6],requestId:args[7],
callId:args[8],clientSessionId:args[9],fingerprint:args[10],nonce:args[11],
promptSha256:args[12],sentPromptSha256:args[13],
descriptorSha256:args[14],auditSha256:args[15],
...(args.length===19?{unobserved:{
requestId:args[16],commandSha256:args[17],promptSha256:args[18]
}}:{})
});
else if(action==="resident-serve-existing-packet"&&args.length===1)
result=await serveExistingPacket(args[0]);
else if(action==="resident-continue-armed-packet"&&args.length===1)
result=await serveExistingPacket(args[0],false,true);
else if(action==="resident-replace-unused-packet"&&args.length===1)
result=await serveExistingPacket(args[0],true);
else if(action==="resident-stop"&&args.length===1)
result=await residentStop(args[0]);
else if(action==="resident-handoff-packet"&&args.length===1)
result=await handoffPacket(args[0]);
else if(action==="resident-takeover-packet"&&args.length===0)
result=await takeoverPacket();
else if(action==="resident-status"&&args.length===1)
result=await residentStatus(args[0]);
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
