import test from 'node:test';
import assert from 'node:assert/strict';
import * as fs from 'node:fs/promises';
import {execFileSync} from 'node:child_process';
import {fileURLToPath} from 'node:url';
import {createHash} from 'node:crypto';
const root=fileURLToPath(new URL('../',import.meta.url));
const a=await import('../skills/codex-pro-dispatch/scripts/parked-activation.mjs');
const AF=Object.getPrototypeOf(async function(){}).constructor;
const parent='01a0ac4a-313b-7f33-9ca0-f76d994ef745';
const meta={threadId:parent,'x-codex-turn-metadata':{turn_id:'turn'}};
const mcp=value=>({content:[{type:'text',text:JSON.stringify(value)}]});
const read=id=>mcp({schemaVersion:1,thread:{id,kind:'chatgpt',status:{type:'idle'}}});

async function fixture(t,legacy=false){
 const data=JSON.parse(execFileSync('python3',['-c',`
import sys,json
sys.path[:0]=['tests','src']
from test_three_feature import ThreeFeatureTests
from codex_pro_dispatch import listener
x=ThreeFeatureTests();x.setUp()
${legacy?'x._enrolled_owner()':''}
p=listener.plan(x.paths,['new-01','new-02'],${legacy?'None':JSON.stringify('Isolated fixture has no old executors')})
x.temp._finalizer.detach()
print(json.dumps({'root':str(x.paths.state_dir.parent),'plan':p}))
`],{cwd:root,encoding:'utf8',env:{...process.env,TMPDIR:'/tmp'}}));
 const previous=process.env.CODEX_PRO_DISPATCH_HOME;
 process.env.CODEX_PRO_DISPATCH_HOME=data.root;
 t.after(async()=>{if(data.close)await data.close();if(previous===undefined)delete process.env.CODEX_PRO_DISPATCH_HOME;else process.env.CODEX_PRO_DISPATCH_HOME=previous;await fs.rm(data.root,{recursive:true,force:true});});
 return data;
}

function tools(g,reads=[]){
 return {
  exec_command:async()=>{throw Error('No shell execution expected in raw stage');},
  write_stdin:async()=>{},
  mcp__codex_app__navigate_to_codex_page:async()=>{},
  mcp__codex_app__send_message_to_thread:async()=>{throw Error('Startup must never send');},
  mcp__codex_app__read_thread:async({threadId})=>{reads.push(threadId);return read(threadId);},
  mcp__node_repl__js:async({code,title})=>{
   const printed=[];
   await new AF('globalThis','nodeRepl','console',code)(g,{requestMeta:meta},{log:value=>printed.push(value)});
   if(title==='Resident Stop qualification'&&code.includes('prepareResidentSupervision')){
    const guard=await import('../skills/codex-pro-dispatch/scripts/resident-supervision.mjs');
    // Simulated host Stop callback, not evidence of live native qualification.
    assert.equal((await guard.stopDecision({hook_event_name:'Stop',session_id:parent,
      turn_id:'turn',stop_hook_active:false})).decision,'block');
   }
   return {content:[{type:'text',text:printed.join('\n')}]};
  },
 };
}

test('native generated startup commits once, saves pinned stages, and sends zero prompts',async t=>{
 const f=await fixture(t),g={},reads=[],printed=[];
 const packet=await a.listenerStartPacket(f.plan,f.root+'/clients');
 await new AF('tools','text',packet.calls.start)(tools(g,reads),v=>printed.push(v));
 assert.deepEqual(reads,['new-01','new-02']);
 const result=printed[0];assert.equal(result.state,'next_action');assert.equal(result.owner.generation,1);
 const raw=await fs.readFile(result.next_packet);
 assert.equal(createHash('sha256').update(raw).digest('hex'),result.next_packet_sha256);
 assert.equal((await fs.stat(result.next_packet)).mode&0o777,0o600);
 const next=JSON.parse(raw);assert.deepEqual(Object.keys(next.calls),['qualify','open','serve']);
 assert.match(next.calls.open,/startup_operation/);
 assert.match(next.calls.qualify,/resident_supervision_probe_required/);
 assert.match(next.calls.serve,/Record joined resident execution/);
 const call=await a.readResidentPacketCall(result.next_packet,'qualify',result.next_packet_sha256);
 assert.equal(call.tool,'functions.exec');assert.equal(call.directNativeFallback,false);
 const before=await fs.readFile(f.root+'/state/resident-owner.json');
 await new AF('tools','text',packet.calls.start)(tools(g),v=>printed.push(v));
 assert.equal(printed[1].reason,'already_committed');
 assert.deepEqual(await fs.readFile(f.root+'/state/resident-owner.json'),before);
 await new AF('tools','text',next.calls.qualify)(tools(g),()=>{});
 await new AF('tools','text',next.calls.open)(tools(g),()=>{});
 f.close=()=>g.parkedSocket.close('fixture_cleanup');
 const bound=JSON.parse(await fs.readFile(f.root+'/state/resident-owner.json','utf8'));
 assert.equal(bound.generation,1);assert.equal(bound.owner,result.owner.owner);
 assert.equal(bound.session.directory,g.parkedResident.directory);
 await assert.rejects(new AF('tools','text',next.calls.open)(tools(g),()=>{}),/Startup acquisition is stale/);
});

test('legacy unknown exclusion returns one restart action without authority mutation',async t=>{
 const f=await fixture(t,true),g={},out=[];
 const before=await fs.readFile(f.root+'/state/resident-owner.json');
 const packet=await a.listenerStartPacket(f.plan,f.root+'/clients');
 await new AF('tools','text',packet.calls.start)(tools(g),v=>out.push(v));
 assert.equal(out[0].state,'blocked');assert.match(out[0].next_action,/Restart the Mac/);
 assert.deepEqual(await fs.readFile(f.root+'/state/resident-owner.json'),before);
});

test('missing executor fails before native identity or any mutation',async t=>{
 const f=await fixture(t),p=await a.listenerStartPacket(f.plan,f.root+'/clients');
 await assert.rejects(new AF('tools','text',p.calls.start)({},()=>{}),/unsupported_listener_surface/);
 await assert.rejects(fs.stat(f.root+'/state/resident-owner.json'),{code:'ENOENT'});
});

test('native task and turn, worker identity, and exact plan bind commit',async t=>{
 const f=await fixture(t),g={};
 await a.captureListenerStart(g,meta,f.plan,f.root+'/clients');
 await assert.rejects(a.commitListenerStart(g,{...meta,threadId:'other'},f.plan,[]),/identity_invalid/);
 await assert.rejects(a.commitListenerStart(g,{...meta,'x-codex-turn-metadata':{turn_id:'later'}},f.plan,[]),/identity_invalid/);
 await assert.rejects(a.commitListenerStart(g,meta,{...f.plan,version:2},[]),/identity_invalid/);
 await assert.rejects(a.commitListenerStart(g,meta,f.plan,[read('wrong'),read('new-02')]),/Worker identity differs/);
 await assert.rejects(fs.stat(f.root+'/state/resident-owner.json'),{code:'ENOENT'});
});

test('CLI packet selects and executes startup across sorted serialization',async t=>{
 const f=await fixture(t);
 const raw=execFileSync('python3',[root+'bin/pro-dispatch','listener','start','--worker-1','new-01',
  '--confirm-quiescent','Isolated fixture has no old executors'],{encoding:'utf8'});
 const p=JSON.parse(raw);assert.equal(p.kind,'native_listener_start_packet');
 assert.match(p.calls.start,/Activation pin changed/);
 const packetFile=f.root+'/cli-start.json';
 await fs.writeFile(packetFile,raw,{mode:0o600});
 const hash=createHash('sha256').update(raw).digest('hex');
 const selected=JSON.parse(execFileSync('node',[root+'skills/codex-pro-dispatch/scripts/parked-activation.mjs',
  'packet-call',packetFile,'start',hash],{encoding:'utf8'}));
 assert.equal(selected.tool,'functions.exec');
 assert.equal(selected.directNativeFallback,false);
 await assert.rejects(fs.stat(f.root+'/state/resident-owner.json'),{code:'ENOENT'});
 const native=tools({}),out=[];
 let loaderCalls=0;
 native.exec_command=async({cmd})=>{
  assert.equal(++loaderCalls,1);assert(cmd.includes(selected.stageFile));
  return {exit_code:0,output:execFileSync('/bin/sh',['-c',cmd],{encoding:'utf8'})};
 };
 await new AF('tools','text',selected.arguments.code)(native,value=>out.push(value));
 assert.equal(out[0].state,'next_action');
 assert.equal(out[0].reason,'owner_acquired');
 assert.equal(loaderCalls,1);
 const reversed=JSON.stringify({...p,execution:Object.fromEntries(Object.entries(p.execution).reverse())});
 await fs.writeFile(packetFile,reversed,{mode:0o600});
 assert.equal((await a.readResidentPacketCall(packetFile,'start',createHash('sha256').update(reversed).digest('hex'))).tool,'functions.exec');
 const swapped={...p.execution,continuation2:p.execution.continuation};delete swapped.continuation;
 for(const execution of [{...p.execution,version:'1'},{...p.execution,extra:true},
  {...p.execution,directNativeFallback:true},swapped,'abcde',5,null,[]]){
  const changed=JSON.stringify({...p,execution});
  await fs.writeFile(packetFile,changed,{mode:0o600});
  await assert.rejects(a.readResidentPacketCall(packetFile,'start',createHash('sha256').update(changed).digest('hex')),
   /Unsupported resident packet call/);
 }
 assert.equal(JSON.parse(await fs.readFile(f.root+'/state/resident-owner.json','utf8')).generation,1);
});
