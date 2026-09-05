"""One bounded provider lifecycle for every generation path."""
import contextlib
import contextvars
import functools
import inspect
import json
import os
import socket
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from uuid import uuid4
from .budget import CallLedger, nonnegative

_scope = contextvars.ContextVar('model_request_scope', default=None)


def positive(value):
    value = nonnegative(value)
    if not value:
        raise ValueError('limit must be positive')
    return value


def integer_limit(value):
    value = positive(value)
    if not value.is_integer():
        raise ValueError("limit must be an integer")
    return int(value)


def output_limit():
    return integer_limit(os.getenv('LLM_MAX_OUTPUT_TOKENS', '16384') or '16384')


def accounted():
    current = _scope.get()
    return bool(current and current[2].get("ledger_backed"))


def current_deadline():
    current = _scope.get()
    return current[1] if current else None


def request_seconds():
    return positive(os.getenv('LLM_REQUEST_DEADLINE_SECONDS', '120') or '120')


@contextlib.contextmanager
def request_scope(user_id=None, deadline=None):
    current = _scope.get()
    end = min(deadline or float('inf'), time.monotonic() + request_seconds())
    token = _scope.set(current or (user_id, end, {}))
    try:
        yield
    finally:
        _scope.reset(token)


def scoped(method):
    signature = inspect.signature(method)
    def scope(args, kwargs):
        bound = signature.bind(*args, **kwargs).arguments
        return request_scope(bound.get('user_id'), bound.get('deadline'))
    if inspect.isgeneratorfunction(method):
        @functools.wraps(method)
        def wrapped(*args, **kwargs):
            with scope(args, kwargs):
                yield from method(*args, **kwargs)
    else:
        @functools.wraps(method)
        def wrapped(*args, **kwargs):
            with scope(args, kwargs):
                return method(*args, **kwargs)
    return wrapped


class Admission:
    def __init__(self, concurrency=8, rpm=120, tpm=2000000):
        self.concurrency, self.rpm, self.tpm = [integer_limit(v) for v in (concurrency, rpm, tpm)]
        self.lock = threading.Lock()
        self.active = set()
        self.window = deque()
        self.cooldown = 0

    def acquire(self, tokens, deadline=None):
        if deadline is None:
            acquired = self.lock.acquire()
        else:
            acquired = self.lock.acquire(timeout=max(0, deadline - time.monotonic()))
        if not acquired:
            raise TimeoutError('model admission deadline exceeded')
        now = time.monotonic()
        try:
            if deadline is not None and now >= deadline:
                raise TimeoutError('model admission deadline exceeded')
            while self.window and self.window[0][0] <= now - 60:
                self.window.popleft()
            if (now < self.cooldown or len(self.active) >= self.concurrency or len(self.window) >= self.rpm
                    or sum(n for _, n in self.window) + tokens > self.tpm):
                raise ValueError('global model capacity unavailable')
            token = uuid4().hex
            self.active.add(token)
            self.window.append((now, tokens))
            return token
        finally:
            self.lock.release()

    def release(self, token):
        with self.lock:
            self.active.discard(token)

    def cool(self, seconds):
        with self.lock:
            self.cooldown = max(self.cooldown, time.monotonic() + seconds)


_global_admission = None
_global_lock = threading.Lock()


def global_admission():
    global _global_admission
    with _global_lock:
        if _global_admission is None:
            _global_admission = Admission(*(os.getenv(k, d) for k, d in
                [('LLM_MAX_CONCURRENCY', '8'), ('LLM_RPM', '120'), ('LLM_TPM', '2000000')]))
        return _global_admission


def usage_of(body):
    usage = body.get('usage')
    if not isinstance(usage, dict) or not all(k in usage for k in ('input_tokens', 'output_tokens')):
        return None
    from .answer import extract_usage
    return extract_usage(body)


class _CallWork:
    """Keep one admission slot until the caller and all bounded background work end."""
    def __init__(self, admission, slot, on_done):
        self.admission, self.slot = admission, slot
        self.on_done = on_done
        self.lock = threading.Lock()
        self.references = 1

    def retain(self):
        with self.lock:
            self.references += 1

    def release(self):
        with self.lock:
            self.references -= 1
            last = self.references == 0
        if last:
            try:
                self.admission.release(self.slot)
            finally:
                self.on_done()

    def run(self, operation, deadline):
        done = threading.Event()
        outcome = {}
        self.retain()
        def execute():
            try:
                outcome['value'] = operation()
            except BaseException as exc:
                outcome['error'] = exc
            finally:
                self.release()
                done.set()
        worker = threading.Thread(target=execute, daemon=True)
        try:
            worker.start()
        except BaseException:
            self.release()
            raise
        if not done.wait(max(0, deadline - time.monotonic())):
            raise TimeoutError('model accounting deadline exceeded')
        if 'error' in outcome:
            raise outcome['error']
        if time.monotonic() >= deadline:
            raise TimeoutError('model accounting deadline exceeded')
        return outcome.get('value')


class ModelRuntime:
    def __init__(self, store=None, pricing=None, admission=None, durable=None):
        from .usage import UsagePricing
        output_limit()
        request_seconds()
        self.pricing = pricing or UsagePricing.from_env()
        self.ledger = CallLedger(store, self.pricing, os.getenv('SYSTEM_MONTHLY_BUDGET_TWD', '0')) if store else None
        self.admission = admission or global_admission()
        self.durable = durable
        self._work_condition = threading.Condition()
        self._work_count = 0
        self._draining = False

    def _work_finished(self):
        with self._work_condition:
            self._work_count -= 1
            self._work_condition.notify_all()

    def _start_work(self, tokens, deadline):
        with self._work_condition:
            if self._draining:
                raise ValueError('model runtime is draining')
            self._work_count += 1
        try:
            slot = self.admission.acquire(tokens, deadline=deadline)
        except BaseException:
            self._work_finished()
            raise
        return _CallWork(self.admission, slot, self._work_finished)

    def drain(self, timeout=0):
        end = time.monotonic() + max(0, timeout)
        with self._work_condition:
            self._draining = True
            while self._work_count:
                remaining = end - time.monotonic()
                if remaining <= 0:
                    return False
                self._work_condition.wait(remaining)
            return True

    def _persist(self):
        if self.durable:
            # Unchanged snapshots are already durable; actual backup failures raise.
            self.durable()

    def reconcile(self, call_id, cost, reference, *, allow_pending=False):
        self.ledger.reconcile(call_id, cost, reference, allow_pending=allow_pending)
        self._persist()

    @contextlib.contextmanager
    def call(self, request, timeout):
        user_id, end, tracking = _scope.get() or (None, time.monotonic() + request_seconds(), {})
        end = min(end, time.monotonic() + positive(timeout))
        payload = json.loads(request.data)
        cap = min(integer_limit(payload.get('max_output_tokens', output_limit())), output_limit())
        payload['max_output_tokens'] = cap
        request.data = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        # UTF-8 bytes bound tokenization conservatively, with room for protocol framing.
        inputs = len(request.data) + 4096
        tokens = inputs + cap
        rate = max(self.pricing.input_usd_per_million, self.pricing.cached_input_usd_per_million,
                   self.pricing.cache_write_usd_per_million)
        charge = (inputs * rate + cap * self.pricing.output_usd_per_million) * self.pricing.usd_to_twd / 1000000
        work = self._start_work(tokens, end)
        call_id = uuid4().hex
        dispatched = False
        reserved = False
        settled = False
        response = None
        response_stack = contextlib.ExitStack()
        timer = None
        expired = threading.Event()
        state = 'failed'
        def finish(terminal, usage=None):
            nonlocal settled
            if not settled:
                settled = True
                def settle():
                    if self.ledger:
                        self.ledger.settle(call_id, terminal, usage)
                    self._persist()
                work.run(settle, end)
        def expire():
            expired.set()
            if response is not None:
                try:
                    response.fp.raw._sock.shutdown(socket.SHUT_RDWR)
                except (AttributeError, OSError):
                    pass
                response.close()
        try:
            if self.ledger:
                def reserve():
                    self.ledger.reserve(call_id, user_id, charge, tokens)
                    self._persist()
                work.run(reserve, end)
                reserved = True
                tracking["ledger_backed"] = True
            remaining = end - time.monotonic()
            if remaining <= 0:
                raise TimeoutError('model request deadline exceeded')
            # A peer can trickle headers before urlopen returns. Bound the caller's wait;
            # an abandoned opener keeps its global slot until its transport closes.
            opened = threading.Event()
            open_lock = threading.Lock()
            opening = {"abandoned": False}
            def open_transport():
                try:
                    connection_time = end - time.monotonic()
                    if connection_time <= 0:
                        raise TimeoutError('model connection deadline exceeded')
                    result = urllib.request.urlopen(request, timeout=connection_time)
                    error = None
                except BaseException as exc:
                    result, error = None, exc
                with open_lock:
                    if opening["abandoned"]:
                        try:
                            if result is not None:
                                result.__exit__(None, None, None)
                            if isinstance(error, urllib.error.HTTPError):
                                error.close()
                        finally:
                            work.release()
                    else:
                        opening.update(response=result, error=error)
                        opened.set()
                        work.release()
            worker = threading.Thread(target=open_transport, daemon=True)
            work.retain()
            dispatched = True
            try:
                worker.start()
            except BaseException:
                work.release()
                raise
            opened.wait(max(0, end - time.monotonic()))
            with open_lock:
                if not opened.is_set():
                    opening["abandoned"] = True
                    raise TimeoutError('model connection deadline exceeded')
                if opening['error'] is not None:
                    raise opening['error']
                response = response_stack.enter_context(opening['response'])
            remaining = end - time.monotonic()
            timer = threading.Timer(max(0, remaining), expire)
            timer.daemon = True
            timer.start()
            def check():
                if expired.is_set() or time.monotonic() >= end:
                    raise TimeoutError('model request deadline exceeded')
            check()
            yield response, finish, check
        except GeneratorExit:
            state = 'disconnect'
            raise
        except TimeoutError:
            state = 'timeout'
            raise
        except urllib.error.HTTPError as exc:
            state = f'http_{exc.code}'
            if exc.code == 429:
                try:
                    delay = min(3600, max(1, nonnegative(exc.headers.get('Retry-After', '60'))))
                except (ValueError, AttributeError):
                    delay = 60
                self.admission.cool(delay)
            exc.close()
            raise
        finally:
            if timer:
                timer.cancel()
            try:
                response_stack.close()
            finally:
                try:
                    if reserved and not settled and dispatched:
                        try:
                            finish(state)
                        except TimeoutError:
                            # Accounting continues under the retained slot; never wait past expiry.
                            pass
                finally:
                    work.release()

    def complete(self, request, timeout):
        with self.call(request, timeout) as (response, finish, check):
            body = json.loads(response.read())
            check()
            status = body.get('status', 'completed')
            finish(status, usage_of(body))
            if status != 'completed':
                raise ValueError('model response ' + str(status))
            body["_ledger_backed"] = self.ledger is not None
            return body

    def stream(self, request, timeout):
        with self.call(request, timeout) as (response, finish, check):
            for raw in response:
                check()
                line = raw.decode('utf-8', 'replace').strip()
                if not line.startswith('data:'):
                    continue
                raw = line[5:].strip()
                if raw == '[DONE]':
                    break
                try:
                    event = json.loads(raw)
                except ValueError:
                    continue
                kind = event.get('type')
                if kind == 'response.output_text.delta' and isinstance(event.get('delta'), str):
                    yield 'delta', event['delta']
                elif kind in ('response.completed', 'response.incomplete', 'response.failed', 'error'):
                    status = kind.removeprefix('response.')
                    usage = usage_of(event.get('response', {}))
                    finish(status, usage)
                    if usage is not None:
                        yield 'usage', usage
                    yield 'terminal', status
                    return
            check()
            finish('eof')
            yield 'terminal', 'eof'
