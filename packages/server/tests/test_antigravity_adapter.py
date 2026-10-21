"""Antigravity protocol, transcript, resume and approval bridge; never real speech."""
import json
import os
import threading
import time
from pathlib import Path

import pytest
from agent_media_core import antigravity, harnesses
from agent_media_server import antigravity_adapter as agy, sessiond, transcript

SID = '1a2b3c4d-1111-2222-3333-123456789abc'
NATIVE = '9a2b3c4d-1111-2222-3333-123456789abc'


def wait(pred):
    end = time.monotonic() + 8
    while time.monotonic() < end:
        if pred(): return
        time.sleep(.02)
    raise AssertionError('adapter did not settle')


@pytest.fixture
def host(tmp_path, monkeypatch):
    # HOME matters: intake can otherwise load the real env file and play on
    # a real phone. No listening checks, and never construct a speech sink.
    for k in list(os.environ):
        if k.startswith(('MEDIA_', 'RELAY_')): monkeypatch.delenv(k)
    for key in ('HOME', 'XDG_STATE_HOME', 'XDG_CACHE_HOME', 'XDG_CONFIG_HOME'):
        d = tmp_path / key; d.mkdir(); monkeypatch.setenv(key, str(d))
    monkeypatch.setenv('GEMINI_CLI_HOME', str(tmp_path / 'HOME'))
    monkeypatch.setenv('MEDIA_AUTO_TITLE', '0')
    monkeypatch.setenv('MEDIA_SESSIOND_CLOSE_GRACE', '.1')
    monkeypatch.setattr(sessiond, 'mem_available_mb', lambda: None)
    spoken = []
    monkeypatch.setattr(agy, 'speak', lambda s, text: spoken.append((s.id, text)))
    fake = tmp_path / 'fake-agy'
    fake.write_text('''#!/usr/bin/env python3
import json,sys,uuid,os
from pathlib import Path
args=sys.argv[1:]
native=args[args.index('--conversation')+1] if '--conversation' in args else str(uuid.uuid4())
store=Path(os.environ['FAKE_AGY_STATE'])/native
count=int(store.read_text()) if store.exists() else 0
def emit(d): print(json.dumps(d),flush=True)
emit({'event':'init','conversation_id':native,'init':{'cwd':os.getcwd()}})
for line in sys.stdin:
 obj=json.loads(line)
 if obj.get('event')!='user': sys.exit(3)
 text=''.join(x['text'] for x in obj['message']['content'])
 count+=1;store.write_text(str(count))
 emit({'event':'step_update','step_update':{'step_type':'user_input','step_index':count*2,'state':'DONE'}})
 reply=f'{count}: {text}'
 emit({'event':'step_update','step_update':{'step_type':'agent_response','step_index':count*2+1,'state':'ACTIVE','text_delta':reply}})
 emit({'event':'step_update','step_update':{'step_type':'agent_response','step_index':count*2+1,'state':'DONE','text_delta':'\\n'}})
 emit({'event':'result','result':{'conversation_id':native,'status':'SUCCESS','response':reply+'\\n','duration_seconds':.01}})
''')
    fake.chmod(0o755)
    state = tmp_path / 'fake-state'; state.mkdir()
    monkeypatch.setenv('FAKE_AGY_STATE', str(state))
    monkeypatch.setenv('MEDIA_SESSIOND_ANTIGRAVITY', str(fake))
    work = tmp_path / 'work'; work.mkdir()
    sup = sessiond.Supervisor()
    yield sup, work, spoken
    sup.shutdown()


def test_start_followup_park_resume_and_readable_identity(host):
    sup, work, spoken = host
    d = sup.start(str(work), 'hello', agent='antigravity')
    sid = d['session']; s = sup.sessions[sid]
    wait(lambda: s.state == 'waiting' and len(spoken) == 1)
    native = s.native_session
    assert native and native != sid
    assert sessiond.read_record(sid)['native_session'] == native
    assert harnesses.harness_of(sid) == 'antigravity'
    assert harnesses.cwd_of(sid) == str(work)
    assert harnesses.first_prompt(sid) == 'hello'
    assert any(r.session == sid and r.harness == 'antigravity' for r in harnesses.stored())
    found, path = transcript.transcript_of(sid)
    assert found == 'antigravity'
    messages, _ = transcript.messages_at(path, harness='antigravity')
    assert [m['role'] for m in messages] == ['user', 'assistant']
    assert messages[1]['parts'][0]['text'].strip() == '1: hello'
    sup.send(sid, 'again')
    wait(lambda: s.state == 'waiting' and len(spoken) == 2)
    sup.park(sid)
    wait(lambda: not s.live)
    restored = sessiond.Session.from_record(sessiond.read_record(sid))
    assert restored.native_session == native
    sup.sessions[sid] = restored
    sup.send(sid, 'after resume')
    wait(lambda: restored.state == 'waiting' and len(spoken) == 3)
    assert restored.native_session == native
    assert spoken[-1] == (sid, '3: after resume\n')
    messages, _ = transcript.messages_at(path, harness='antigravity')
    assert len(messages) == 6


def test_no_claude_flags_or_control_protocol(host):
    sup, work, _ = host
    s = sessiond.Session(SID, str(work), agent='antigravity')
    s.native_session = NATIVE
    argv = sup._argv(s, 'agy', True)
    assert argv[argv.index('--conversation') + 1] == NATIVE
    assert '--session-id' not in argv and '--resume' not in argv
    assert '--permission-prompt-tool' not in argv
    assert '--dangerously-skip-permissions' not in argv


def test_phone_permission_bridge_approves_once_and_records_tool(host):
    sup, work, _ = host
    d = sup.start(str(work), 'hello', agent='antigravity'); s = sup.sessions[d['session']]
    wait(lambda: s.state == 'waiting')
    answers = []
    payload = {'stepIdx': 7, 'toolCall': {'name': 'run_command', 'args': {'CommandLine': 'echo hello', 'Cwd': str(work)}}}
    t = threading.Thread(target=lambda: answers.append(agy.approval(sup, s.id, payload)))
    t.start(); wait(lambda: bool(s.pending))
    rid = next(iter(s.pending))
    assert s.pending[rid]['request']['tool_name'] == 'Bash'
    assert s.pending[rid]['request']['input']['command'] == 'echo hello'
    sup.answer(s.id, rid, {'behavior': 'allow'})
    t.join(3)
    assert answers == [{'decision': 'auto_approve'}]
    agy.approval(sup, s.id, payload, stage='post')
    messages, _ = transcript.messages_at(str(antigravity.path(s.id)), harness='antigravity')
    tools = [p for m in messages for p in m['parts'] if p['type'] == 'tool']
    assert tools and tools[-1]['status'] == 'done'


def test_failed_result_is_visible_but_not_spoken(host):
    sup, work, spoken = host
    s = sessiond.Session(SID, str(work), agent='antigravity')
    sup.sessions[SID] = s
    agy.event(sup, s, {'event': 'result', 'result': {'status': 'ERROR', 'error': 'authentication required'}})
    assert not spoken
    assert s.last_result['is_error'] is True
    assert 'authentication required' in antigravity.path(SID).read_text()


def test_hook_install_preserves_other_hooks_and_unmanaged_sessions(host, monkeypatch):
    p = Path(os.environ['GEMINI_CLI_HOME']) / '.gemini/config/hooks.json'
    p.parent.mkdir(parents=True)
    p.write_text(json.dumps({'existing': {'enabled': False}}))
    agy.install_hook(); agy.install_hook()
    d = json.loads(p.read_text())
    assert d['existing'] == {'enabled': False}
    assert set(d['agent-media-phone']) == {'PreToolUse', 'PostToolUse'}
    monkeypatch.delenv('MEDIA_SESSIOND_SESSION', raising=False)
    assert agy.hook({'toolCall': {'name': 'run_command'}}) == {'decision': 'allow'}


def test_antigravity_settings_never_offer_claude_models(host, monkeypatch):
    from agent_media_server import driver, session_settings
    from agent_media_server.driver import headless
    sup, work, _ = host
    s = sessiond.Session(SID, str(work), agent="antigravity")
    sup.sessions[SID] = s
    sup._save(s)
    monkeypatch.setattr(headless, "call", lambda op, **kw: {"ok": True, **s.view()})
    st = session_settings._answer(SID)
    assert st["agent"] == "antigravity"
    assert st["models"] == []
    assert st["can"] == {"model": False, "plan": False}
    assert driver.for_new("antigravity").kind == "headless"
    assert driver.for_session(SID).kind == "headless"


def test_question_card_returns_answers_without_a_native_terminal_prompt(host):
    from agent_media_server.driver.headless import approval_of
    sup, work, _ = host
    s = sup.sessions[sup.start(str(work), 'hello', agent='antigravity')['session']]
    wait(lambda: s.state == 'waiting')
    answers = []
    payload = {'stepIdx': 8, 'toolCall': {'name': 'ask_question', 'args': {
        'questions': [{'question': 'Which colour?', 'options': ['Blue', 'Green']}]}}}
    t = threading.Thread(target=lambda: answers.append(agy.approval(sup, s.id, payload)))
    t.start(); wait(lambda: bool(s.pending))
    rid = next(iter(s.pending))
    card = approval_of(s.pending[rid])
    assert card['agent'] == 'antigravity' and card['kind'] == 'question'
    assert card['questions'][0]['options'][0]['label'] == 'Blue'
    sup.answer(s.id, rid, {'behavior': 'allow', 'updatedInput': {'answers': {'Which colour?': 'Blue'}}})
    t.join(3)
    assert answers[0]['decision'] == 'deny'
    assert 'Blue' in answers[0]['reason'] and 'user answered' in answers[0]['reason']


def test_denied_command_does_not_get_auto_approval(host):
    sup, work, _ = host
    s = sup.sessions[sup.start(str(work), 'hello', agent='antigravity')['session']]
    wait(lambda: s.state == 'waiting')
    answers = []
    payload = {'stepIdx': 9, 'toolCall': {'name': 'run_command', 'args': {'CommandLine': 'echo nope'}}}
    t = threading.Thread(target=lambda: answers.append(agy.approval(sup, s.id, payload)))
    t.start(); wait(lambda: bool(s.pending))
    sup.answer(s.id, next(iter(s.pending)), {'behavior': 'deny', 'message': 'Do not run it'})
    t.join(3)
    assert answers == [{'decision': 'deny', 'reason': 'Do not run it'}]
