"""Antigravity's documented stream protocol and phone approval hook bridge.

No Claude control messages are sent to agy. Native IDs are persisted separately
from app IDs, and only successful final responses go through speech intake.
"""
from __future__ import annotations
import json
import os
import shlex
import sys
import threading
import uuid
from pathlib import Path

from agent_media_core import antigravity as store

SAFE = {'read_file', 'list_directory', 'view_file', 'list_dir', 'find_by_name', 'grep_search', 'sed_file', 'list_permissions'}
TOOLS = {'read_file': 'Read', 'list_directory': 'LS', 'run_command': 'Bash', 'write_to_file': 'Write', 'write_file': 'Write',
         'replace_file_content': 'Edit', 'multi_replace_file_content': 'Edit', 'edit_file': 'Edit',
         'view_file': 'Read', 'list_dir': 'LS', 'find_by_name': 'Glob', 'grep_search': 'Grep',
         'ask_question': 'AskUserQuestion'}


def _ask_option(o: object) -> dict:
    if isinstance(o, dict):
        return {'label': str(o.get('label') or ''), 'description': str(o.get('description') or '')}
    s = str(o or '').strip()
    desc = ''
    if s.startswith('(Recommended)'):
        s = s[len('(Recommended)'):].strip()
        desc = 'Recommended'
    return {'label': s, 'description': desc}


def tool_input(call: dict) -> tuple[str, dict]:
    name = str(call.get('name') or '')
    args = dict(call.get('args') or {})
    for old, new in [('CommandLine', 'command'), ('Cwd', 'cwd'), ('AbsolutePath', 'file_path'),
                     ('TargetFile', 'file_path'), ('Path', 'file_path'), ('CodeContent', 'content'),
                     ('Content', 'content'), ('Target', 'old_string'), ('Replacement', 'new_string'),
                     ('Query', 'pattern'), ('SearchPath', 'path')]:
        if old in args:
            args[new] = args.pop(old)
    if name == 'ask_question':
        args = {'questions': [{'question': q.get('question', ''), 'header': str(q.get('header') or ''),
                              'multiSelect': bool(q.get('is_multi_select') or q.get('multiSelect')),
                              'options': [_ask_option(o) for o in q.get('options') or []]}
                             for q in args.get('questions') or [] if isinstance(q, dict)]}
    return TOOLS.get(name, name), args


def install_hook() -> None:
    """Merge our named hook, preserving all unrelated hooks and policy."""
    # GEMINI_CLI_HOME isolates tests; the actual CLI uses the normal home.
    home = Path(os.environ.get('GEMINI_CLI_HOME') or Path.home())
    p = home / '.gemini/config/hooks.json'
    p.parent.mkdir(parents=True, exist_ok=True)
    data = json.loads(p.read_text()) if p.exists() and p.read_text().strip() else {}
    data['agent-media-phone'] = {'PreToolUse': [{'matcher': '*', 'hooks': [{
        'type': 'command', 'command': shlex.join([sys.executable, '-m', __name__, 'pre']), 'timeout': 660}]}]}
    data['agent-media-phone']['PostToolUse'] = [{'matcher': '*', 'hooks': [{'type': 'command', 'command': shlex.join([sys.executable, '-m', __name__, 'post']), 'timeout': 10}]}]
    import tempfile
    fd, tmp = tempfile.mkstemp(dir=p.parent, prefix='.hooks-')
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, p)
    finally:
        if os.path.exists(tmp): os.unlink(tmp)


def _is_auto(permissions_val: str = '', mode_val: str = '', sid: str = '') -> bool:
    from . import permissions as perms, sessiond
    if sid and (sessiond.state_root() / f'{sid}.auto').exists():
        return True
    return mode_val == 'auto' or perms.mode(permissions_val) == perms.NORMAL


def hook(payload: dict) -> dict:
    sid = os.environ.get('MEDIA_SESSIOND_SESSION', '')
    if not sid:
        return {'decision': 'allow'}
    stage = sys.argv[1] if len(sys.argv) > 1 else 'pre'
    if stage == 'pre':
        call_obj = payload.get('toolCall') or {}
        name = str(call_obj.get('name') or '')
        tool, args = tool_input(call_obj)
        if tool != 'AskUserQuestion':
            from . import sessiond
            rec = sessiond.read_record(sid) or {}
            if _is_auto(str(rec.get('permissions') or ''), str(rec.get('mode') or ''), sid):
                tid = f'agy-{payload.get("stepIdx", "")}'
                store.append(sid, {'cwd': str(rec.get('cwd') or ''), 'type': 'assistant',
                                   'message': {'role': 'assistant',
                                               'content': [{'type': 'tool_use', 'id': tid, 'name': tool, 'input': args}]}})
                return {'decision': 'auto_approve'}
    from .driver.headless import call
    r = call('antigravity_hook', session=sid, payload=payload, stage=stage, timeout=620)
    return r.get('hook') if r.get('ok') else {'decision': 'deny', 'reason': 'The session host could not confirm approval. Ask the user before retrying.'}


def approval(supervisor, session: str, payload: dict, stage: str = 'pre') -> dict:
    call = payload.get('toolCall') or {}
    name = str(call.get('name') or '')
    with supervisor.cond:
        s = supervisor._get(session)
        if s.agent != 'antigravity':
            from .sessiond import Refused
            raise Refused('not an Antigravity session', 'bad_request')
        tool, args = tool_input(call)
        tid = f'agy-{payload.get("stepIdx", "")}'
        if stage == 'post':
            transcript(s, {'type': 'user', 'message': {'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': tid, 'content': str(payload.get('error') or ''), 'is_error': bool(payload.get('error'))}]}})
            return {}
        transcript(s, {'type': 'assistant', 'message': {'role': 'assistant', 'content': [{'type': 'tool_use', 'id': tid, 'name': tool, 'input': args}]}})
        if name in SAFE:
            return {'decision': 'allow'}
        if tool != 'AskUserQuestion' and _is_auto(s.permissions, s.mode, s.id):
            return {'decision': 'auto_approve'}
        if tool == 'AskUserQuestion':
            threading.Thread(target=_ask_pre, args=(s, tid, args), daemon=True, name='agy-ask-pre').start()
        rid = 'agy_' + uuid.uuid4().hex
        supervisor._on_event(s, {'type': 'control_request', 'request_id': rid,
                                'request': {'subtype': 'can_use_tool', 'tool_name': tool,
                                            'input': args, 'tool_use_id': tid}})
        s.pending[rid]['antigravity'] = True
        supervisor._save(s)
        done = supervisor.cond.wait_for(lambda: rid in s.responses or not s.live, 600)
        response = s.responses.pop(rid, None)
        s.pending.pop(rid, None)
        if not s.pending and s.state == 'approval': s.set_state('working')
        supervisor._save(s)
        if not done or not response:
            if tool == 'AskUserQuestion':
                transcript(s, {'type': 'user', 'message': {'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': tid, 'content': 'Approval expired or the conversation ended.', 'is_error': True}]}})
                threading.Thread(target=_ask_post, args=(s, tid, args, {}), daemon=True, name='agy-ask-post').start()
            return {'decision': 'deny', 'reason': 'Approval expired or the conversation ended.'}
        if tool == 'AskUserQuestion' and response.get('behavior') == 'allow':
            answers = (response.get('updatedInput') or {}).get('answers') or {}
            reason = 'The user answered from the app: ' + json.dumps(answers) + '. Use these answers; do not ask the same question again.'
            transcript(s, {'type': 'user', 'message': {'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': tid, 'content': reason, 'is_error': False}]}})
            threading.Thread(target=_ask_post, args=(s, tid, args, answers), daemon=True, name='agy-ask-post').start()
            return {'decision': 'deny', 'reason': reason}
        if tool == 'AskUserQuestion':
            msg = response.get('message') or 'Declined from the app.'
            transcript(s, {'type': 'user', 'message': {'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': tid, 'content': msg, 'is_error': True}]}})
            threading.Thread(target=_ask_post, args=(s, tid, args, {}), daemon=True, name='agy-ask-post').start()
            return {'decision': 'deny', 'reason': msg}
        if response.get('behavior') == 'allow':
            return {'decision': 'auto_approve'}
        return {'decision': 'deny', 'reason': response.get('message') or 'Declined from the app.'}


def _ask_pre(s, tid: str, args: dict) -> None:
    try:
        from agent_media_core.intake.hook_claude_code import _handle_pretooluse
        _handle_pretooluse({
            'session_id': s.id,
            'cwd': s.cwd,
            'tool_name': 'AskUserQuestion',
            'tool_input': args,
            'tool_use_id': tid,
            'transcript_path': str(store.path(s.id)),
        }, harness='antigravity')
    except Exception:
        pass


def _ask_post(s, tid: str, args: dict, answers: dict) -> None:
    try:
        from agent_media_core.intake.hook_claude_code import _handle_posttooluse
        _handle_posttooluse({
            'session_id': s.id,
            'cwd': s.cwd,
            'tool_name': 'AskUserQuestion',
            'tool_input': args,
            'tool_use_id': tid,
            'tool_response': {'answers': answers} if answers else {},
        }, harness='antigravity')
    except Exception:
        pass


def transcript(s, obj: dict) -> None:
    store.append(s.id, {'cwd': s.cwd, **obj})


def speak(s, text: str) -> None:
    import hashlib
    from agent_media_core.intake._hook_stdin import run
    from agent_media_core.intake._text import strip_markdown
    from agent_media_core.types import Source
    cleaned = strip_markdown(text).strip()
    if not cleaned:
        return
    dedup_key = hashlib.sha1(cleaned.encode('utf-8')).hexdigest()
    meta: dict = {
        'session': s.id,
        'tmux': s.workspace,
        'kind': 'stop',
        'voice_from': 'session',
        'dedup_key': dedup_key,
    }
    try:
        from agent_media_core import session_settings
        sp = session_settings.speak_priority(s.id)
        if sp is not None:
            meta['speak_priority'] = sp
        if session_settings.toast(s.id):
            meta['toast'] = True
    except Exception:
        pass
    run(Source.ANTIGRAVITY, 'ANTIGRAVITY', text=text, metadata=meta)


def event(supervisor, s, obj: dict) -> None:
    """Translate native events into the existing session-state vocabulary."""
    native = str(obj.get('conversation_id') or (obj.get('result') or {}).get('conversation_id')
                 or (obj.get('step_update') or {}).get('conversation_id') or '')
    if native:
        import re
        if re.fullmatch(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}', native):
            s.native_session = native
            supervisor._save(s)
    import time
    s.last_event_at = time.time()
    kind = obj.get('event')
    if kind == 'init':
        s.agy_ready = True
        supervisor._on_event(s, {'type': 'system', 'subtype': 'init', 'model': s.model,
                                'permissionMode': 'default'})
        if not s.agy_queue:
            s.set_state('waiting')
            supervisor._save(s)
    elif kind == 'step_update':
        step = obj.get('step_update') or {}
        stype = str(step.get('step_type') or '')
        index = str(step.get('step_index', ''))
        if stype == 'user_input' and step.get('state') == 'DONE':
            if s.agy_inputs:
                uid, text = s.agy_inputs.pop(0)
                transcript(s, {'type': 'user', 'uuid': uid, 'message': {'role': 'user', 'content': [{'type': 'text', 'text': text}]}})
            try:
                from agent_media_core.sinks.speech import end_session_speech_cut, session_reply_read
                end_session_speech_cut(s.id)
                session_reply_read(s.id)
            except Exception:
                pass
            s.set_state('working')
            supervisor._save(s)
        elif stype == 'agent_response':
            text = str(step.get('text_delta') or '')
            if text:
                s.agy_text[index] = s.agy_text.get(index, '') + text
            if step.get('state') == 'DONE':
                text = s.agy_text.pop(index, '')
                if text.strip():
                    s.agy_replied = True
                    if not hasattr(s, 'agy_reply'):
                        s.agy_reply = []
                    s.agy_reply.append(text)
                    transcript(s, {'type': 'assistant', 'message': {'role': 'assistant', 'content': [{'type': 'text', 'text': text}]}})
        else:
            # Unknown native steps remain visible as activity, without
            # pretending their payload is a completed tool result.
            supervisor._on_event(s, {'type': 'antigravity_step', **step})
    elif kind == 'result':
        s.agy_had_result = True
        result = obj.get('result') or {}
        status = str(result.get('status') or '')
        resp_text = str(result.get('response') or '')
        if not resp_text and getattr(s, 'agy_reply', None):
            resp_text = '\n\n'.join(s.agy_reply)
        ok = status in ('SUCCESS', 'DONE') or bool(s.agy_replied or resp_text.strip())
        text = resp_text
        if not text and result.get('error'):
            text = str(result['error'])
        # A result is authoritative, including CLIs that don't stream text.
        if not s.agy_replied and text:
            transcript(s, {'type': 'assistant', 'message': {'role': 'assistant', 'content': [{'type': 'text', 'text': text}]}})
        transcript(s, {'type': 'system', 'subtype': 'turn_duration'})
        s.agy_text.clear()
        if hasattr(s, 'agy_reply'):
            s.agy_reply.clear()
        s.agy_replied = False
        # Stream input has no message UUID receipts. One result consumes the
        # oldest submitted message; queued messages remain explicitly tracked.
        if s.agy_queue:
            s.agy_queue.pop(0)
        s.queued = set(s.agy_queue[1:])
        supervisor._on_event(s, {'type': 'result', 'subtype': status.lower(),
                                'is_error': not ok, 'result': text,
                                'duration_ms': float(result.get('duration_seconds') or 0) * 1000})
        if s.agy_queue:
            s.set_state('working')
            supervisor._save(s)
        if ok and text.strip():
            threading.Thread(target=speak, args=(s, text), daemon=True, name='agy-speech').start()


if __name__ == '__main__':
    try:
        print(json.dumps(hook(json.load(sys.stdin))))
    except Exception:
        print(json.dumps({'decision': 'deny', 'reason': 'The approval hook could not complete.'}))
