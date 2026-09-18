import test from "node:test";
import assert from "node:assert/strict";
import * as fs from "node:fs/promises";
import {watch} from "node:fs";
import {tmpdir} from "node:os";
import {dirname} from "node:path";
import {execFile} from "node:child_process";
import {createHash} from "node:crypto";
import {fileURLToPath} from "node:url";
import * as vm from "node:vm";
import {openSession} from "../skills/codex-pro-dispatch/scripts/parked-socket.mjs";

const root=fileURLToPath(new URL("../",import.meta.url));
const scripts=root+"skills/codex-pro-dispatch/scripts/";
const helper=scripts+"pro-dispatch",author=scripts+"parked-activation.mjs";
const AF=Object.getPrototypeOf(async function(){}).constructor;
const plain=v=>JSON.parse(JSON.stringify(v));
async function missing(path){
await assert.rejects(fs.lstat(path),e=>e.code==="ENOENT");
}
async function tree(directory){
const out={};
async function visit(at,prefix){
for(const name of await fs.readdir(at)){
const path=at+"/"+name,key=prefix+name,s=await fs.lstat(path);
if(s.isDirectory())await visit(path,key+"/");
else if(s.isFile())out[key]=await fs.readFile(path);
}
}
await visit(directory,"");return out;
}
function appeared(path){
return new Promise((resolve,reject)=>{
let done=false,busy=false,again=false,deferred=false;
const w=watch(dirname(path),()=>{again=true;void check();});
const timer=setTimeout(()=>end(Error("Missing unit event: "+path)),10000);
function end(e,v){
if(done)return;done=true;clearTimeout(timer);w.close();e?reject(e):resolve(v);
}
async function check(){
if(done||busy)return;busy=true;again=false;
let torn=false;
try{end(null,JSON.parse(await fs.readFile(path,"utf8")));}
catch(e){
if(e.code!=="ENOENT"&&e.code!=="EAGAIN"&&!(e instanceof SyntaxError))end(e);
else if(e.code==="EAGAIN"||e instanceof SyntaxError)torn=true;
}finally{
busy=false;
if(done)return;
if(again){deferred=false;void check();return;}
if(torn&&!deferred){deferred=true;setImmediate(()=>void check());}
}
}
w.on("error",e=>end(e));void check();
});
}

async function fixture(t){
const base=process.platform==="darwin"?"/private/tmp":tmpdir();
const d=await fs.realpath(await fs.mkdtemp(base+"/qr-"));
const home=d+"/authority",oldDir=d+"/old";
const env={...process.env,CODEX_PRO_DISPATCH_HOME:home,PYTHONDONTWRITEBYTECODE:"1"};
const pending=new Set(),commands=[],native={reads:[],sends:[],navigations:[]};
const broker="unit-broker",parent="unit-parent",worker="unit-pro",rid="queued-A2";
const clientSession="unit-client",meta={threadId:broker,
"x-codex-turn-metadata":{turn_id:"new-turn"}};
let old,sent=null;
const runtime={parkedOpenAttempts:new Set(["earlier-open-attempt"])};
function execute(file,args){
const p=new Promise(resolve=>{
const child=execFile(file,args,{env,timeout:35000,maxBuffer:8388608},(error,out,err)=>{
let value=null;try{value=JSON.parse(error?err:out);}catch{}
resolve({code:error?(Number.isInteger(error.code)?error.code:1):0,
value,stdout:out,stderr:err,output:out+err});
});
child.stdin?.end();
});
pending.add(p);p.then(()=>pending.delete(p));return p;
}
async function cli(args){
const r=await execute("python3",[helper,...args]);
assert.equal(r.code,0,r.stderr||r.stdout);return r.value;
}
const activate=(...args)=>execute(process.execPath,[author,...args]);
t.after(async()=>{
if(old)await old.close("unit_cleanup");
if(runtime.parkedSocket&&runtime.parkedSocket!==old)
await runtime.parkedSocket.close("unit_cleanup");
await Promise.allSettled([...pending]);
await fs.rm(d,{recursive:true});
});
await cli(["worker","set","--conversation-id",worker,
"--confirm-worker","--native-controls-confirmed"]);
await fs.mkdir(oldDir,{mode:448});
old=await openSession(oldDir,{helper,configDir:home+"/config",stateDir:home+"/state",
worker,parent,leaseMs:60000,idleMs:10000,replyMs:30000});
const binding=Object.freeze({broker,turn:"old-turn"});
Object.assign(runtime,{parkedSocket:old,parkedBinding:binding,parkedDelivery:null});
const prompt=d+"/prompt.txt",raw=Buffer.from("Review café.\r\nPreserve exact input.\n","utf8");
await fs.writeFile(prompt,raw,{mode:384});
// Actual rendezvous, CLI submission and socket. Only the failed job result is synthetic.
const initial=activate("rendezvous",oldDir,"1",rid,prompt,clientSession);
const gate=await activate("command-ready",oldDir,"1",rid);
assert.equal(gate.code,0,gate.stderr);
const delivery=await old.receive();assert(delivery);
assert.equal(delivery.requestId,rid);
await old.finish(delivery.callId,{request_id:rid,observation:"blocked"});
const failed=await initial;
assert.equal(failed.code,0,failed.stderr);
assert.equal(failed.value.state,"queued");
assert.equal(failed.value.wake.status,"blocked");
await old.close();
const queued=await cli(["queue","collect",rid]);
const command=JSON.parse(await fs.readFile(oldDir+"/command-1.json","utf8"));
const proof={sessionId:old.config.sessionId,requestId:rid,fingerprint:queued.fingerprint,
callId:delivery.callId,clientSessionId:clientSession,nonce:command.nonce,
promptSha256:createHash("sha256").update(raw).digest("hex")};
assert.equal(proof.promptSha256,command.promptSha256);
const ancestor=d+"/ancestor";
await fs.mkdir(ancestor,{mode:448});
const ancestorMarker=ancestor+"/replacement-open.once.json";
await fs.writeFile(ancestorMarker,JSON.stringify({unitAncestor:true,next:proof.sessionId}),
{flag:"wx",mode:384});
function generate(changes={}){
const p={...proof,...changes};
return activate("closed-queued-packet",broker,parent,worker,oldDir,
p.sessionId,p.requestId,p.fingerprint,p.callId,p.clientSessionId,p.nonce,p.promptSha256);
}
const textResult=text=>({isError:false,content:[{type:"text",text}]});
const tools={
async exec_command(args){
assert.equal(args.login,false);assert.equal(args.tty,false);
assert.equal(args.yield_time_ms,30000);
const r=await execute("/bin/sh",["-c","exec "+args.cmd]);
commands.push({command:args.cmd,...r});
return {output:r.output,exit_code:r.code};
},
async mcp__node_repl__js(args){
assert([30000,60000].includes(args.timeout_ms));
const lines=[];
// Resolve persistent names from the live fixture namespace, including a value
// assigned during this call. Snapshot parameters cannot model REPL globals.
await new AF("nodeRepl","globalThis","console",
"with (globalThis) {\n"+args.code+"\n}")(
{tmpDir:d,requestMeta:meta},runtime,{log:(...v)=>lines.push(v.map(String).join(" "))});
return textResult(lines.join("\n"));
},
async mcp__codex_app__read_thread(args){
assert.deepEqual(plain(args),{threadId:worker,turnLimit:2,maxOutputCharsPerItem:20000});
const id=sent?.match(/^\[CODEX_PRO_DISPATCH assignment_id=([^\]]+)\]\n/)?.[1];
const value={schemaVersion:1,thread:{id:worker,kind:"chatgpt",status:{type:"idle"}},
turns:sent?[{id:"turn-"+id,items:[
{id:"turn-"+id,type:"userMessage",content:[{type:"text",text:sent}]},
{id:"answer-"+id,type:"agentMessage",text:
"[CODEX_PRO_DISPATCH_RESULT assignment_id="+id+"]\nunit answer\n"+
"[CODEX_PRO_DISPATCH_END assignment_id="+id+"]"}
]}]:[]};
const raw=JSON.stringify(value,null,2)+"\n";
native.reads.push(raw);return textResult(raw);
},
async mcp__codex_app__send_message_to_thread(args){
assert.equal(args.threadId,worker);
assert.deepEqual(Object.keys(args).sort(),["prompt","threadId"]);
const id=args.prompt.match(/^\[CODEX_PRO_DISPATCH assignment_id=([^\]]+)\]\n/)?.[1];
assert(id);
const s=(await cli(["status",id])).assignment;
assert.equal(s.status,"armed");assert.equal(s.no_resend,true);
assert.equal(s.submission_count,0);assert.equal(s.parent_task_id,parent);
sent=args.prompt;native.sends.push(plain(args));return textResult(JSON.stringify({threadId:worker}));
},
async mcp__codex_app__navigate_to_codex_page(args){
assert.equal(args.threadId,parent);native.navigations.push(plain(args));
return textResult(JSON.stringify({threadId:parent}));
}
};
async function run(p,which="open"){
const output=[];
const context=vm.createContext({tools,setTimeout,clearTimeout,text:v=>output.push(v)});
await vm.runInContext("(async()=>{\n"+p.calls[which]+"\n})()",context);
let acknowledgment=null;
if(which==="dispatch"&&output.length===2){
acknowledgment=plain(output.shift());
assert.equal(acknowledgment.kind,"native_send_acknowledged");
assert.equal(acknowledgment.worker_conversation_id,worker);
assert.equal(acknowledgment.no_resend,true);
assert.equal(acknowledgment.outbound_readback_verified,false);
assert.equal(typeof acknowledgment.evidence_file,"string");
}
assert.equal(output.length,1);
const result=output[0].content?JSON.parse(output[0].content[0].text):plain(output[0]);
if(acknowledgment)assert.equal(acknowledgment.request_id,result.request_id);
return result;
}
const names=async()=>(await fs.readdir(d)).filter(n=>n.startsWith("pro-session-"));
return {d,home,oldDir,old,binding,runtime,meta,broker,parent,worker,rid,clientSession,
prompt,proof,ancestorMarker,cli,activate,execute,generate,run,names,commands,native};
}

// UNIT ONLY: real private storage/CLI/socket; native execution context is supplied above.
test("closed queued replacement preserves immediate and ancestor evidence",async t=>{
const f=await fixture(t),before=await tree(f.oldDir),state=await tree(f.home);
const ancestor=await fs.readFile(f.ancestorMarker);
const legacy=await f.activate("closed-packet",f.broker,f.parent,f.worker,f.oldDir);
assert.notEqual(legacy.code,0);
const p=await f.generate();assert.equal(p.code,0,p.stderr);
const opened=await f.run(p.value);
assert.equal(opened.replacedSessionId,f.proof.sessionId);
assert.notEqual(opened.sessionId,f.proof.sessionId);
assert.notEqual(opened.directory,f.oldDir);
assert.notEqual(f.runtime.parkedSocket.config.token,f.old.config.token);
assert.deepEqual(f.runtime.parkedSocket.config.queuedResume,f.proof);
assert.deepEqual(f.runtime.parkedBinding,{broker:f.broker,turn:"new-turn"});
assert.equal(f.runtime.parkedDelivery,null);
assert(f.runtime.parkedOpenAttempts.has("earlier-open-attempt"));
for(const [name,raw] of Object.entries(before))
assert.deepEqual(await fs.readFile(f.oldDir+"/"+name),raw);
assert.deepEqual(await fs.readFile(f.ancestorMarker),ancestor);
const marker=JSON.parse(await fs.readFile(f.oldDir+"/replacement-open.once.json","utf8"));
assert.equal(marker.previousSessionId,f.proof.sessionId);
assert.deepEqual(marker.previousBinding,f.binding);
assert.deepEqual(marker.queuedResume,f.proof);
assert.equal(marker.attempt,p.value.openAttempt);
await missing(f.oldDir+"/wake.sock");await missing(opened.directory+"/ready-1.json");
assert.deepEqual(await tree(f.home),state);
assert.equal(f.native.sends.length,0);assert.equal((await f.names()).length,1);
});

test("concurrent closed queued opens allow one replacement only",async t=>{
const f=await fixture(t),a=await f.generate(),b=await f.generate();
assert.equal(a.code,0);assert.equal(b.code,0);
const packets=[a.value,b.value],out=await Promise.allSettled(packets.map(p=>f.run(p)));
assert.equal(out.filter(r=>r.status==="fulfilled").length,1);
assert.equal(out.filter(r=>r.status==="rejected").length,1);
await assert.rejects(f.run(packets[out.findIndex(r=>r.status==="fulfilled")]));
assert.notEqual((await f.generate()).code,0);
assert.equal((await f.names()).length,1);
assert.equal(f.native.sends.length,0);
});

test("closed queued proof rejects altered audit, command, readiness and prompt",async t=>{
const f=await fixture(t),state=await tree(f.home);
for(const [name,change] of [
["transport-audit.json",v=>{v.events[0].callId="0".repeat(32);}],
["transport-audit.json",v=>{v.events[1].disposition="pending";}],
["transport-audit.json",v=>{v.events.push({...v.events[0]});}],
["command-observed-1.json",v=>{v.nonce="0".repeat(32);}],
["command-1.json",v=>{v.requestId="different-request";}],
["ready-1.json",v=>{v.ordinal=2;}]
]){
const path=f.oldDir+"/"+name,raw=await fs.readFile(path),v=JSON.parse(raw);
change(v);await fs.writeFile(path,JSON.stringify(v));
assert.notEqual((await f.generate()).code,0,name);
await fs.writeFile(path,raw);
}
const prompt=f.oldDir+"/command-1/prompt.txt",raw=await fs.readFile(prompt);
await fs.writeFile(prompt,Buffer.concat([raw,Buffer.from("changed")]));
assert.notEqual((await f.generate()).code,0);
await fs.writeFile(prompt,raw);
await missing(f.oldDir+"/replacement-open.once.json");
assert.deepEqual(await f.names(),[]);assert.deepEqual(await tree(f.home),state);
});

test("new canonical receipt after packet generation prevents replacement",async t=>{
const f=await fixture(t),p=await f.generate();assert.equal(p.code,0,p.stderr);
await f.cli(["prepare","--assignment-id",f.rid,"--parent-task-id","other-parent",
"--native-controls-confirmed","--prompt-file",f.prompt]);
const state=await tree(f.home);
await assert.rejects(f.run(p.value),/Queued-resume check/);
await missing(f.oldDir+"/replacement-open.once.json");
assert.equal(f.runtime.parkedSocket,f.old);assert.deepEqual(await f.names(),[]);
assert.deepEqual(await tree(f.home),state);assert.equal(f.native.sends.length,0);
});

test("changed closed evidence after generation cannot consume replacement marker",async t=>{
const f=await fixture(t),p=await f.generate();assert.equal(p.code,0,p.stderr);
const path=f.oldDir+"/command-observed-1.json";
const v=JSON.parse(await fs.readFile(path,"utf8"));v.nonce="0".repeat(32);
await fs.writeFile(path,JSON.stringify(v));
await assert.rejects(f.run(p.value),/evidence changed/);
await missing(f.oldDir+"/replacement-open.once.json");
assert.equal(f.runtime.parkedSocket,f.old);assert.equal(f.runtime.parkedBinding,f.binding);
assert(f.runtime.parkedOpenAttempts.has(p.value.openAttempt));
assert.deepEqual(await f.names(),[]);
});

test("wrong broker and retained delivery still block closed queued replacement",async t=>{
const f=await fixture(t),p=await f.generate();assert.equal(p.code,0,p.stderr);
f.meta.threadId="another-broker";
await assert.rejects(f.run(p.value),/Broker identity mismatch/);
f.meta.threadId=f.broker;f.runtime.parkedDelivery={requestId:f.rid};
await assert.rejects(f.run(p.value),/not recoverable/);
await missing(f.oldDir+"/replacement-open.once.json");
assert.equal(f.runtime.parkedSocket,f.old);assert.deepEqual(await f.names(),[]);
});

test("explicit queued resume makes one first send and reuses the listener for B",async t=>{
const f=await fixture(t),p=await f.generate();
assert.equal(p.code,0,p.stderr);
const queuePath=f.home+"/state/queue/"+f.rid+".json";
const original=await fs.readFile(queuePath);
const opened=await f.run(p.value),session=opened.directory;
const socket=f.runtime.parkedSocket,token=socket.config.token;
const client=(operation,rid,...args)=>f.execute(process.execPath,[
scripts+"parked-client.mjs",session,operation,rid,...args
]);
const ordinary=await client("submit",f.rid,f.prompt,f.clientSession);
assert.equal(ordinary.code,0,ordinary.stderr);
assert.equal(ordinary.value.state,"queued");
assert.equal(ordinary.value.wake.status,"collect_only");
await missing(session+"/ready-1.json");
const earlyB=await f.activate("rendezvous",session,"2","request-B",
f.prompt,f.clientSession);
assert.notEqual(earlyB.code,0);
await missing(session+"/command-2.json");
await missing(session+"/command-2");
const resumed=f.activate("resume",session,"1",f.rid);
const command=await appeared(session+"/command-1.json");
assert.equal(command.operation,"resume");
assert.equal(command.requestId,f.rid);
assert.equal(command.fingerprint,f.proof.fingerprint);
assert.equal(command.promptSha256,f.proof.promptSha256);
assert.notEqual(command.nonce,f.proof.nonce);
await missing(session+"/command-1/prompt.txt");
await missing(session+"/ready-1.json");
const gate=await f.activate("command-ready",session,"1",f.rid);
assert.equal(gate.code,0,gate.stderr);
assert.equal(gate.value.commandReady,true);
assert.equal(gate.value.operation,"resume");
assert.equal(gate.value.nonce,command.nonce);
await missing(session+"/ready-1.json");
assert.deepEqual(await fs.readFile(queuePath),original);
const received=await f.run(p.value,"receive");
assert.equal(received.delivery.requestId,f.rid);
assert.equal(received.delivery.operation,"run");
assert.deepEqual(received.delivery,plain(f.runtime.parkedDelivery));
const consumed=JSON.parse(await fs.readFile(session+"/queued-resume.once.json","utf8"));
assert.deepEqual(consumed,command);
// This duplicate reaches the actual client while the first delivery is held,
// before any dispatch receipt exists. Its exclusive wake marker must reject it.
const duplicate=await client("resume",f.rid,"1",command.nonce);
assert.notEqual(duplicate.code,0);
assert.deepEqual(await fs.readFile(queuePath),original);
assert.equal(f.native.sends.length,0);
const completed=await f.run(p.value,"dispatch");
assert.equal(completed.kind,"runner_receipt");
assert.equal(completed.state,"published");
assert.equal(completed.ok,true);
assert.equal(f.native.navigations.length,1);
assert.equal(completed.result,undefined); // Full answers stay on the client transport.
assert.equal(f.runtime.parkedDelivery,null);
const answer=await resumed;
assert.equal(answer.code,0,answer.stderr);
assert.equal(answer.value.answer.payload,"unit answer");
assert.equal(answer.value.answer.verification_level,"bounded_native_summary");
assert.equal(answer.value.answer.source_bytes_verified,false);
assert.equal(answer.value.answer.generation_finality_verified,false);
assert.equal(answer.value.fingerprint,f.proof.fingerprint);
assert.equal(answer.value.parent_task_id,f.parent);
assert.equal(answer.value.worker_conversation_id,f.worker);
const first=(await f.cli(["status",f.rid])).assignment;
assert.equal(first.submission_count,1);
assert.equal(first.no_resend,true);
assert.equal(first.outbound_prompt_verified,true);
assert.equal(f.native.sends.length,1);
assert.equal(f.commands.filter(r=>r.command.includes("'arm'")).length,1);
const collected=await client("collect",f.rid);
assert.equal(collected.code,0,collected.stderr);
assert.deepEqual(collected.value.answer,answer.value.answer);
const repeatedSubmit=await client("submit",f.rid,f.prompt,f.clientSession);
assert.equal(repeatedSubmit.code,0,repeatedSubmit.stderr);
assert.deepEqual(repeatedSubmit.value.answer,answer.value.answer);
assert.equal(repeatedSubmit.value.wake.status,"collect_only");
assert.notEqual((await f.activate("resume",session,"1",f.rid)).code,0);
assert.equal(f.native.sends.length,1);
await missing(session+"/ready-2.json");

const b=f.activate("rendezvous",session,"2","request-B",f.prompt,f.clientSession);
const gateB=await f.activate("command-ready",session,"2","request-B");
assert.equal(gateB.code,0,gateB.stderr);
assert.equal(gateB.value.commandReady,true);
assert.equal(gateB.value.operation,undefined);
await missing(session+"/ready-2.json");
const second=await f.run(p.value,"receive");
assert.equal(second.delivery.requestId,"request-B");
assert.equal(second.delivery.operation,"run");
const doneB=await f.run(p.value,"dispatch");
assert.equal(doneB.state,"published");
assert.equal(doneB.ok,true);
assert.equal(f.native.navigations.length,2);
const answerB=await b;
assert.equal(answerB.code,0,answerB.stderr);
assert.equal(answerB.value.answer.payload,"unit answer");
assert.equal((await f.cli(["status","request-B"])).assignment.submission_count,1);
assert.equal((await f.cli(["status",f.rid])).assignment.submission_count,1);
assert.equal(f.native.sends.length,2);
assert.equal(f.commands.filter(r=>r.command.includes("'arm'")).length,2);
assert.equal(f.runtime.parkedSocket,socket);
assert.equal(socket.config.sessionId,opened.sessionId);
assert.equal(socket.config.token,token);
assert.equal(f.runtime.parkedDelivery,null);
assert.equal((await f.names()).length,1);
await socket.close("unit_two_jobs_complete");
const audit=JSON.parse(await fs.readFile(session+"/transport-audit.json","utf8"));
assert.deepEqual(audit.events.filter(e=>e.name==="accepted").map(e=>e.requestId),
[f.rid,"request-B"]);
assert.deepEqual(audit.events.filter(e=>e.name==="finished").map(e=>e.disposition),
["published","published"]);
assert.equal(new Set(audit.events.filter(e=>e.name==="accepted").map(e=>e.callId)).size,2);
await missing(session+"/wake.sock");
});

test("duplicate gates and altered observation cannot produce a queued resume",async t=>{
const f=await fixture(t),p=await f.generate();
assert.equal(p.code,0,p.stderr);
const opened=await f.run(p.value),session=opened.directory,state=await tree(f.home);
const resumed=f.activate("resume",session,"1",f.rid);
await appeared(session+"/command-1.json");
assert.notEqual((await f.activate("resume",session,"1",f.rid)).code,0);
const gates=await Promise.all([
f.activate("command-ready",session,"1",f.rid),
f.activate("command-ready",session,"1",f.rid)
]);
assert.equal(gates.filter(r=>r.code===0).length,1);
assert.equal(gates.filter(r=>r.code!==0).length,1);
const path=session+"/command-observed-1.json";
const observed=JSON.parse(await fs.readFile(path,"utf8"));
observed.nonce=f.proof.nonce;
await fs.writeFile(path,JSON.stringify(observed));
const receiving=f.run(p.value,"receive");
receiving.catch(()=>{});
const result=await resumed;
assert.notEqual(result.code,0);
await missing(session+"/queued-resume.once.json");
assert.equal(f.native.sends.length,0);
assert.deepEqual(await tree(f.home),state);
await f.runtime.parkedSocket.close("unit_invalid_observation");
assert.equal((await receiving).delivery,null);
const audit=JSON.parse(await fs.readFile(session+"/transport-audit.json","utf8"));
assert.deepEqual(audit.events,[]);
await missing(session+"/ready-2.json");
});

test("a receipt appearing after command-ready blocks wake even when already armed",async t=>{
for(const armed of [false,true]){
const f=await fixture(t),p=await f.generate();
assert.equal(p.code,0,p.stderr);
const opened=await f.run(p.value),session=opened.directory;
const resumed=f.activate("resume",session,"1",f.rid);
const gate=await f.activate("command-ready",session,"1",f.rid);
assert.equal(gate.code,0,gate.stderr);
await missing(session+"/ready-1.json");
await f.cli(["prepare","--assignment-id",f.rid,"--parent-task-id",f.parent,
"--native-controls-confirmed","--prompt-file",f.prompt]);
if(armed)await f.cli(["arm",f.rid]);
const before=await tree(f.home);
const receiving=f.run(p.value,"receive");
receiving.catch(()=>{});
const result=await resumed;
assert.notEqual(result.code,0);
await missing(session+"/queued-resume.once.json");
assert.deepEqual(await tree(f.home),before);
const receipt=(await f.cli(["status",f.rid])).assignment;
assert.equal(receipt.status,armed?"armed":"prepared");
assert.equal(receipt.submission_count,0);
if(armed)assert.equal(receipt.no_resend,true);
assert.equal(f.native.sends.length,0);
assert.equal(f.native.reads.length,0);
await f.runtime.parkedSocket.close("unit_receipt_race");
assert.equal((await receiving).delivery,null);
const audit=JSON.parse(await fs.readFile(session+"/transport-audit.json","utf8"));
assert.deepEqual(audit.events,[]);
}
});

test("expired resume command stops without receive, resubmission or rearming",async t=>{
const f=await fixture(t),p=await f.generate();
assert.equal(p.code,0,p.stderr);
const opened=await f.run(p.value),session=opened.directory,before=await tree(f.home);
const resumed=f.activate("resume",session,"1",f.rid);
const path=session+"/command-1.json",command=await appeared(path);
// Unit-only expired evidence exercises gate validation, not host-duration proof.
command.deadlineAt=Date.now()-1;
await fs.writeFile(path,JSON.stringify(command));
const gate=await f.activate("command-ready",session,"1",f.rid);
assert.notEqual(gate.code,0);
await missing(session+"/command-observed-1.json");
await missing(session+"/ready-1.json");
await missing(session+"/queued-resume.once.json");
await f.runtime.parkedSocket.close("unit_expired_command");
assert.notEqual((await resumed).code,0);
assert.notEqual((await f.activate("resume",session,"1",f.rid)).code,0);
assert((await fs.lstat(session+"/command-1")).isDirectory());
assert.deepEqual(await tree(f.home),before);
assert.equal(f.native.sends.length,0);
assert.equal((await f.names()).length,1);
const audit=JSON.parse(await fs.readFile(session+"/transport-audit.json","utf8"));
assert.deepEqual(audit.events,[]);
await missing(session+"/ready-2.json");
});

test("retained-config mismatch or a recreated socket path defeats closure proof",async t=>{
for(const kind of ["retained-config","socket-path"]){
const f=await fixture(t),p=await f.generate();
assert.equal(p.code,0,p.stderr);
const before=await tree(f.home);
if(kind==="retained-config"){
f.old.config.worker="different-retained-worker";
}else{
await fs.writeFile(f.oldDir+"/wake.sock","not an absent socket",{flag:"wx",mode:384});
}
await assert.rejects(f.run(p.value));
await missing(f.oldDir+"/replacement-open.once.json");
assert.equal(f.runtime.parkedSocket,f.old);
assert.equal(f.runtime.parkedBinding,f.binding);
assert.deepEqual(await f.names(),[]);
assert.deepEqual(await tree(f.home),before);
assert.equal(f.native.sends.length,0);
}
});
