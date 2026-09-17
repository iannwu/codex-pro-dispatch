import test from "node:test";
import assert from "node:assert/strict";
import * as fs from "node:fs/promises";
import {tmpdir} from "node:os";
import {fileURLToPath} from "node:url";

const root=fileURLToPath(new URL("../",import.meta.url));
const script=root+"skills/codex-pro-dispatch/scripts/parked-activation.mjs";
const activation=await import(script);
const AF=Object.getPrototypeOf(async function(){}).constructor;
const OLD="01a09f71-aa77-7502-8c1c-0d2ea8264ae9",NEW="01a0ac4a-313b-7f33-9ca0-f76d994ef745";
const expected={generation:1,owner:"owner",parent:OLD,worker_pool_sha256:"a".repeat(64),configDir:"/missing/config",stateDir:"/missing/state"};
const mcp=v=>({content:[{type:"text",text:JSON.stringify(v)}]});

test("takeover packet captures only native identity then performs one owner read",async t=>{
 const directory=await fs.mkdtemp(tmpdir()+"/takeover-");await fs.chmod(directory,0o700);
 t.after(()=>fs.rm(directory,{recursive:true,force:true}));
 const source=await fs.readFile(script),hash=(await import("node:crypto")).createHash("sha256").update(source).digest("hex");
 const call=activation.buildTakeoverCall(expected,hash);
 const g={},meta={threadId:NEW,"x-codex-turn-metadata":{turn_id:"turn"}},calls=[],printed=[];
 const tools={
  async mcp__node_repl__js(arg){calls.push(arg.title);const lines=[];
   await new AF("globalThis","nodeRepl","console",arg.code)(g,{requestMeta:meta,tmpDir:directory},{log:v=>lines.push(String(v))});
   return {content:[{type:"text",text:lines.join("\n")}]}},
  async mcp__codex_app__read_thread(arg){calls.push("read");assert.deepEqual(arg,{threadId:OLD,turnLimit:1,includeOutputs:false});
   return mcp({schemaVersion:1,thread:{id:OLD,kind:"codex",hostId:"local",status:{type:"idle"}}});},
 };
 await new AF("tools","text",call)(tools,value=>printed.push(value));
 assert.deepEqual(calls,["Capture takeover identity","read","Commit takeover"]);
 assert.equal(printed[0].outcome,"commit_unknown");
 const evidence=await fs.readdir(directory);assert.equal(evidence.length,1);
 const evidenceDir=directory+"/"+evidence[0],nested=await fs.readdir(evidenceDir);assert.deepEqual(nested,["owner-read.json"]);
 const evidencePath=evidenceDir+"/owner-read.json",info=await fs.stat(evidencePath),dirInfo=await fs.stat(evidenceDir);
 assert.equal(info.mode&0o777,0o600);assert.equal(dirInfo.mode&0o777,0o700);
});

test("identity export rejects shell supplied or malformed task identity",async()=>{
 await assert.rejects(activation.captureTakeoverIdentity({}, {threadId:"not-a-uuid"}, expected),/identity_invalid/);
 assert.deepEqual(await activation.captureTakeoverIdentity({}, {threadId:OLD,"x-codex-turn-metadata":{turn_id:"turn"}}, expected),{outcome:"already_owner"});
});

async function nativeFixture(t,capacity=2) {
 const {execFileSync}=await import("node:child_process");
 const fixture=JSON.parse(execFileSync("python3",["-c",`
import sys,json
sys.path[:0]=["tests","src"]
from test_three_feature import ThreeFeatureTests
from codex_pro_dispatch import core,resident
activate=core.activate_worker_pool
core.activate_worker_pool=lambda workers,**kw: activate(workers[:${capacity}],**kw)
x=ThreeFeatureTests(); x.setUp(); owner,c=x._enrolled_owner()
owner=resident.control("inspect",{},x.paths)["owner"]
owner["parent"]=${JSON.stringify(OLD)}
with core.state_lock(x.paths) as lock: resident.write(x.paths,lock,owner)
x.temp._finalizer.detach()
print(json.dumps(dict(root=x.temp.name,expected=dict(generation=owner["generation"],owner=owner["owner"],parent=owner["parent"],worker_pool_sha256=owner["worker_pool_sha256"],configDir=str(x.paths.config_dir),stateDir=str(x.paths.state_dir)))))
`],{cwd:root,encoding:"utf8"}));
 t.after(()=>fs.rm(fixture.root,{recursive:true,force:true}));
 return fixture;
}

async function runNative(expected,directory,{changeIdentity=false,missing=false}={}) {
 const source=await fs.readFile(script),hash=(await import("node:crypto")).createHash("sha256").update(source).digest("hex");
 const g={},meta=missing?{}:{threadId:NEW,"x-codex-turn-metadata":{turn_id:"turn"}},calls=[],printed=[];
 const raw=' {"schemaVersion":1,"thread":{"id":'+JSON.stringify(expected.parent)+',"kind":"codex","hostId":"local","status":{"type":"idle"}}}\n';
 const tools={
  async mcp__node_repl__js(arg){calls.push(arg.title);const lines=[];
   try {await new AF("globalThis","nodeRepl","console",arg.code)(g,{requestMeta:meta,tmpDir:directory},{log:v=>lines.push(String(v))});}
   catch(e){return {isError:true,content:[{type:"text",text:String(e.message)}]};}
   return {content:[{type:"text",text:lines.join("\n")}]}},
  async mcp__codex_app__read_thread(arg){calls.push("read");assert.deepEqual(arg,{threadId:expected.parent,turnLimit:1,includeOutputs:false});
   if(changeIdentity)meta.threadId=OLD;
   return {content:[{type:"text",text:raw}]};},
 };
 await new AF("tools","text",activation.buildTakeoverCall(expected,hash))(tools,x=>printed.push(x));
 return {calls,result:printed[0],raw};
}

test("T16 real helper commits captured identity and exact evidence, replay reads nothing",async t=>{
 const fixture=await nativeFixture(t),directory=fixture.root;
 const run=await runNative(fixture.expected,directory);
 assert.equal(run.result.outcome,"committed",JSON.stringify(run.result));
 assert.deepEqual(run.calls,["Capture takeover identity","read","Commit takeover"]);
 const owner=JSON.parse(await fs.readFile(fixture.expected.stateDir+"/resident-owner.json","utf8"));
 assert.equal(owner.parent,NEW);
 assert.equal(await fs.readFile(owner.qualification.takeover.evidence_path,"utf8"),run.raw);
 assert.ok(owner.qualification.takeover.evidence_path.startsWith(directory+"/pro-takeover-"));
 const replay=await runNative({...fixture.expected,parent:NEW,generation:owner.generation,owner:owner.owner},directory);
 assert.equal(replay.result.outcome,"already_owner");
 assert.deepEqual(replay.calls,["Capture takeover identity"]);
});

test("T16 missing metadata and changed identity normalize before evidence or commit",async t=>{
 const fixture=await nativeFixture(t),path=fixture.expected.stateDir+"/resident-owner.json";
 const before=await fs.readFile(path);
 for(const options of [{missing:true},{changeIdentity:true}]){
  const run=await runNative(fixture.expected,fixture.root,options);
  assert.equal(run.result.outcome,"identity_invalid");
  assert.equal(run.calls.includes("read"),!options.missing);
  assert.deepEqual(await fs.readFile(path),before);
 }
 await assert.rejects(activation.captureTakeoverIdentity({},undefined,fixture.expected),/identity_invalid/);
 assert.equal((await fs.readdir(fixture.root)).some(x=>x.startsWith("pro-takeover-")),false);
});

test("T16 chmod immediately follows mkdtemp, failure precedes open and helper",async()=>{
 const source=await fs.readFile(script,"utf8");
 const body=source.slice(source.indexOf("export async function commitNativeTakeover"),source.indexOf("export function buildTakeoverCall")).replace("export ","");
 const order=[],fakeFs={async mkdtemp(path){order.push(["mkdtemp",path]);return "/native/pro-takeover-fixture";},
  async chmod(path,mode){order.push(["chmod",path,mode]);throw Error("chmod denied");},
  async open(){throw Error("must not open");}};
 const commit=await new AF("fs","J","join","dir","execFile",body+";return commitNativeTakeover;")(fakeFs,JSON.stringify,()=>"",root,()=>{throw Error("must not spawn");});
 const g={},meta={threadId:NEW,"x-codex-turn-metadata":{turn_id:"turn"}};
 await activation.captureTakeoverIdentity(g,meta,expected);
 await assert.rejects(commit(g,meta,expected,"raw","/native"),/chmod denied/);
 assert.deepEqual(order,[["mkdtemp","/native/pro-takeover-"],["chmod","/native/pro-takeover-fixture",0o700]]);
});

test("T12 pending collector permits only idle sibling and never invokes prepared send",async()=>{
 for(const capacity of [1,2]){
  const calls=[];
  const hooks={openCollector:async()=>calls.push("open"),closeCollector:async()=>calls.push("close"),
   collect:async request=>{calls.push(request);return {ok:true,observation:"pending"};},
   endCollected:async()=>{throw Error("pending slot cannot end");},
   sendPrepared:async()=>{throw Error("inherited request cannot send");}};
  const run=activation.recoverPoolRequests(activation.poolRecoveryPlan({recovery:["inherited"],preparedRecovery:[]},capacity),hooks);
  if(capacity===1)await assert.rejects(run,/Recovery remains collect-only/); else await run;
  assert.deepEqual(calls,["open","inherited","close"]);
 }
});

for(const inherited of ["armed","prepared"]){
 test(`T12/T13 real runner after takeover: ${inherited} retained or cancelled, fresh sends once`,async t=>{
  const fixture=await nativeFixture(t,inherited==="prepared"?1:2),e=fixture.expected;
  const {execFileSync,execFile}=await import("node:child_process");
  const python=code=>JSON.parse(execFileSync("python3",["-c",`
import sys,json
from pathlib import Path
sys.path[:0]=["src"]
from codex_pro_dispatch import core,resident
from codex_pro_dispatch.queue import Queue
p=core.RuntimePaths(Path(sys.argv[1]),Path(sys.argv[2]))
q=Queue(p)
v=resident.control("inspect",{},p)["owner"]
c={k:v[k] for k in ("generation","owner","parent","worker_pool_sha256")}
${code}
`,e.configDir,e.stateDir],{cwd:root,encoding:"utf8"}));
  const prior=python(`q.submit("inherited",b"original prompt","client")
token=resident.invocation.set(dict(c,invocation="original",request="inherited"))
claim=q.claim(c["parent"],True,"inherited")
${inherited==="armed"?'core.arm_for_send("slot-a","inherited",c["generation"],"original",p)':''}
resident.invocation.reset(token)
print(json.dumps(claim))`);
  assert.equal((await runNative(e,fixture.root)).result.outcome,"committed");
  const prepared=python(`resident.control("settle",c,p)
started=resident.control("start",c,p)
v=resident.control("inspect",{},p)["owner"]
c={k:v[k] for k in ("generation","owner","parent","worker_pool_sha256")}
q.submit("fresh",b"fresh prompt","client")
print(json.dumps(dict(credentials=c,workers=[dict(slot=x["slot"],conversation_id=x["worker_conversation_id"]) for x in v["slots"]],started=started,answer=core.result_marker("fresh")+"\\nfresh answer\\n"+core.end_marker("fresh"))))`);
  const sends=[],commands=[],prompts=new Map([["worker-a",prior.wrapped_prompt]]),g={};
  const cli=async args=>JSON.parse(execFileSync("python3",[root+"bin/pro-dispatch",...args],{encoding:"utf8",env:{...process.env,CODEX_PRO_DISPATCH_HOME:e.configDir.slice(0,-7)}}));
  const tools={
   async exec_command(args){commands.push(args.cmd);return await new Promise(resolve=>{
    execFile("/bin/sh",["-c",args.cmd],{env:{...process.env,CODEX_PRO_DISPATCH_HOME:e.configDir.slice(0,-7)}},(err,out,stderr)=>resolve({output:out||stderr,exit_code:err?.code||0}));});},
   async mcp__node_repl__js(args){const lines=[];await new AF("globalThis","nodeRepl","console",args.code)(g,{tmpDir:fixture.root},{log:x=>lines.push(x)});return {content:[{type:"text",text:lines.join("\n")}]};},
   async mcp__codex_app__read_thread({threadId}){
    const prompt=prompts.get(threadId),sent=sends.includes(threadId);
    const items=prompt?[{id:"turn",type:"userMessage",content:[{type:"text",text:prompt}]}]:[];
    if(sent)items.push({id:"answer",type:"agentMessage",text:prepared.answer});
    return mcp({schemaVersion:1,thread:{id:threadId,kind:"chatgpt",status:{type:"idle"}},turns:items.length?[{id:"turn",items}]:[]});
   },
   async mcp__codex_app__send_message_to_thread({threadId,prompt}){
    assert.equal(sends.length,0);
    const receipt=JSON.parse(await fs.readFile(e.stateDir+"/assignments/fresh.json","utf8"));
    assert.equal(receipt.status,"armed");assert.equal(receipt.no_resend,true);
    assert.equal(threadId,inherited==="armed"?"worker-b":"worker-a");
    sends.push(threadId);prompts.set(threadId,prompt);return mcp({});
   },
  };
  const runner=await fs.readFile(root+"skills/codex-pro-dispatch/scripts/parked-runner.js","utf8");
  await new AF("globalThis","tools","text",runner.replace("globalThis.describeFailure =", "const describeFailure ="))(g,tools,()=>{});
  const config={helper:root+"bin/pro-dispatch",configDir:e.configDir,stateDir:e.stateDir,
   workers:prepared.workers,parent:NEW,preflightConfirmed:true,restoreParent:false,maxSnapshots:1};
  await activation.recoverPoolRequests(activation.poolRecoveryPlan({recovery:prepared.started.request_ids,preparedRecovery:[]},2),{
   openCollector:()=>cli(["resident","collector-open",JSON.stringify(prepared.credentials)]),
   closeCollector:()=>cli(["resident","collector-close",JSON.stringify({...prepared.credentials,collector_only:true})]),
   collect:request=>g.runParkedJob({...config,parent:OLD,collectOnly:true,residentInvocation:{...prepared.credentials,collector_only:true,request,request_parent:OLD}},request),
   endCollected:()=>{throw Error("Inherited pending request cannot end");},
  });
  assert.equal(sends.length,0);
  const result=await g.runParkedJob({...config,residentInvocation:{...prepared.credentials,invocation:"fresh-inv",request:"fresh"}},"fresh");
  assert.equal(result.observation,"published",JSON.stringify(result));
  assert.equal(sends.length,1);
  assert.equal(commands.filter(x=>x.includes("'arm-for-send'")).length,1);
  const old=python('print(json.dumps(core.load_assignment("inherited",p)))');
  assert.equal(old.status,inherited==="armed"?"armed":"abandoned");
  assert.equal(old.submission_count,0);
 });
}
