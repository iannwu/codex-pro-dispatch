import test from "node:test";
import assert from "node:assert/strict";
import * as fs from "node:fs/promises";
import {tmpdir} from "node:os";
import {execFile} from "node:child_process";
import {fileURLToPath} from "node:url";
import {openSession} from "../skills/codex-pro-dispatch/scripts/parked-socket.mjs";

const root=fileURLToPath(new URL("../",import.meta.url));
const scripts=root+"skills/codex-pro-dispatch/scripts/";
const helper=scripts+"pro-dispatch";
const author=scripts+"parked-activation.mjs";
const AF=Object.getPrototypeOf(async function(){}).constructor;

async function absent(path){
await assert.rejects(fs.lstat(path),e=>e.code==="ENOENT");
}

async function fixture(t,closed=true){
const base=process.platform==="darwin"?"/private/tmp":tmpdir();
const d=await fs.realpath(await fs.mkdtemp(base+"/pro-closed-unit-"));
const home=d+"/authority",oldDir=d+"/old";
const env={...process.env,CODEX_PRO_DISPATCH_HOME:home};
async function execute(file,args){
return await new Promise((resolve,reject)=>{
const child=execFile(file,args,{env,timeout:15000,maxBuffer:8388608},(error,out,err)=>{
try{resolve({code:error?error.code:0,value:JSON.parse(error?err:out)});}
catch(e){reject(Error(err||out||String(e)));}
});
child.stdin?.end();
});
}
const configured=await execute("python3",[helper,"worker","set",
"--conversation-id","unit-pro","--confirm-pro","--native-controls-confirmed"]);
assert.equal(configured.code,0,JSON.stringify(configured.value));
await fs.mkdir(oldDir,{mode:448});
const old=await openSession(oldDir,{
helper,configDir:home+"/config",stateDir:home+"/state",
worker:"unit-pro",parent:"unit-parent",
leaseMs:60000,idleMs:30,replyMs:10000
});
const binding=Object.freeze({broker:"unit-broker",turn:"old-turn"});
const runtime={parkedSocket:old,parkedBinding:binding,parkedDelivery:null};
if(closed){
assert.equal(await old.receive(),null);
await old.close();
}
t.after(async()=>{
await old.close("unit_cleanup");
if(runtime.parkedSocket&&runtime.parkedSocket!==old)
await runtime.parkedSocket.close("unit_cleanup");
await fs.rm(d,{recursive:true});
});
const generate=(action="closed-packet")=>execute(process.execPath,[
author,action,"unit-broker","unit-parent","unit-pro",
...(action==="closed-packet"?[oldDir]:[])
]);
async function run(p){
const output=[];
const tools={
async mcp__node_repl__js(args){
assert.equal(args.timeout_ms,60000);
const lines=[];
await new AF("nodeRepl","globalThis","console",args.code)(
{
tmpDir:d,
requestMeta:{
threadId:"unit-broker",
"x-codex-turn-metadata":{turn_id:"new-turn"}
}
},
runtime,{log:value=>lines.push(String(value))}
);
return {isError:false,content:[{type:"text",text:lines.join("\n")}]};
}
};
await new AF("tools","text",p.calls.open)(tools,value=>output.push(value));
assert.equal(output.length,1);
return JSON.parse(output[0].content[0].text);
}
const freshDirectories=async()=>(await fs.readdir(d)).filter(n=>n.startsWith("pro-session-"));
return {d,home,oldDir,old,binding,runtime,generate,run,freshDirectories};
}

test("closed idle listener can be explicitly replaced while preserving old evidence",async t=>{
const f=await fixture(t);
const names=await fs.readdir(f.oldDir),before={};
for(const name of names)before[name]=await fs.readFile(f.oldDir+"/"+name);
const normal=await f.generate("packet");
assert.equal(normal.code,0);
await assert.rejects(f.run(normal.value),/Do not reopen session/);
const generated=await f.generate();
assert.equal(generated.code,0,JSON.stringify(generated.value));
assert.equal(generated.value.previous.sessionId,f.old.config.sessionId);
const result=await f.run(generated.value);
assert.equal(result.phase,"listener_open_not_yet_waiting");
assert.equal(result.replacedSessionId,f.old.config.sessionId);
assert.notEqual(result.sessionId,f.old.config.sessionId);
assert.notEqual(result.directory,f.oldDir);
assert.equal(f.runtime.parkedBinding.broker,"unit-broker");
assert.equal(f.runtime.parkedBinding.turn,"new-turn");
assert.equal(f.runtime.parkedDelivery,null);
for(const name of names)
assert.deepEqual(await fs.readFile(f.oldDir+"/"+name),before[name]);
await absent(f.oldDir+"/wake.sock");
await absent(result.directory+"/ready-1.json");
const marker=JSON.parse(await fs.readFile(f.oldDir+"/replacement-open.once.json","utf8"));
assert.equal(marker.previousSessionId,f.old.config.sessionId);
assert.deepEqual(marker.previousBinding,f.binding);
assert.equal(marker.attempt,generated.value.openAttempt);
assert.equal((await f.freshDirectories()).length,1);
});

test("concurrent and repeated replacement cannot open a second listener",async t=>{
const f=await fixture(t);
const a=await f.generate(),b=await f.generate();
assert.equal(a.code,0);assert.equal(b.code,0);
const packets=[a.value,b.value];
const results=await Promise.allSettled(packets.map(p=>f.run(p)));
assert.equal(results.filter(r=>r.status==="fulfilled").length,1);
assert.equal(results.filter(r=>r.status==="rejected").length,1);
const succeeded=results.findIndex(r=>r.status==="fulfilled");
await assert.rejects(f.run(packets[succeeded]));
assert.notEqual((await f.generate()).code,0);
assert.equal((await f.freshDirectories()).length,1);
});

test("absence of closure evidence leaves the old listener untouched",async t=>{
const f=await fixture(t,false);
const result=await f.generate();
assert.notEqual(result.code,0);
assert.equal(f.runtime.parkedSocket,f.old);
assert.equal(f.runtime.parkedBinding,f.binding);
await absent(f.oldDir+"/transport-audit.json");
await absent(f.oldDir+"/replacement-open.once.json");
assert((await fs.lstat(f.oldDir+"/wake.sock")).isSocket());
assert.deepEqual(await f.freshDirectories(),[]);
});

test("evidence changed after packet generation fails before replacement",async t=>{
const f=await fixture(t);
const generated=await f.generate();
assert.equal(generated.code,0);
const auditPath=f.oldDir+"/transport-audit.json";
const audit=JSON.parse(await fs.readFile(auditPath,"utf8"));
audit.events.push({name:"accepted",requestId:"unit-conflict"});
await fs.writeFile(auditPath,JSON.stringify(audit));
await assert.rejects(f.run(generated.value),/evidence changed/);
assert.equal(f.runtime.parkedSocket,f.old);
assert.equal(f.runtime.parkedBinding,f.binding);
await absent(f.oldDir+"/replacement-open.once.json");
assert.deepEqual(await f.freshDirectories(),[]);
assert.notEqual((await f.generate()).code,0);
});

test("retained delivery prevents replacement even with a closed unused audit",async t=>{
const f=await fixture(t);
const generated=await f.generate();
assert.equal(generated.code,0);
f.runtime.parkedDelivery={requestId:"unresolved"};
await assert.rejects(f.run(generated.value),/not recoverable/);
assert.equal(f.runtime.parkedSocket,f.old);
assert.equal(f.runtime.parkedBinding,f.binding);
await absent(f.oldDir+"/replacement-open.once.json");
assert.deepEqual(await f.freshDirectories(),[]);
});
