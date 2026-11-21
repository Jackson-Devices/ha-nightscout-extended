"""Read-only HTTP replay for the synthetic Nightscout fixtures."""

import json
from collections import Counter
from contextlib import contextmanager
from datetime import datetime
from email.utils import format_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Lock, Thread
from time import sleep
from urllib.parse import parse_qsl, urlsplit


WATCHED_HEADERS = ('api-secret', 'authorization', 'cookie', 'proxy-authorization')


def epoch(value):
    try:
        return int(datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp() * 1000)
    except ValueError:
        # The fixture deliberately includes dirty records.
        return value


def document(key, records, wire):
    row = dict(records[key])
    identifier = row.pop('id', key)
    at = row.pop('at', None)
    uploaded = row.pop('uploaded', None)
    if wire == 'v3':
        row.setdefault('identifier', identifier)
        row.setdefault('app', 'synthetic-contract')
        if at is not None:
            row['date'] = epoch(at)
        if uploaded is not None:
            row['srvCreated'] = epoch(uploaded)
    else:
        row['_id'] = identifier
        if at is not None and 'type' in row and 'eventType' not in row:
            row.update(date=epoch(at), dateString=at)
        elif at is not None:
            row['created_at'] = at
        if uploaded is not None:
            row['uploaded_at'] = uploaded
    return row


def reply(data, key, wire):
    if key in data['lists']:
        if wire not in ('v1', 'v3'):
            raise ValueError(f'{key} requires a collection route')
        body = [document(item, data['records'], wire) for item in data['lists'][key]]
        if wire == 'v3':
            body = {'status': 200, 'result': body}
        return 200, {}, 0, json.dumps(body).encode('utf-8')
    if key in data['objects']:
        return 200, {}, 0, json.dumps(data['objects'][key]).encode('utf-8')
    fault = data['faults'][key]
    if 'raw' in fault:
        body = fault['raw']
    else:
        body = json.dumps({'status': fault['status']})
    return fault['status'], fault.get('headers', {}), fault.get('delay', 0), body.encode('utf-8')


class Plan:
    def __init__(self, data, step, clock):
        self.lock = Lock()
        self.date = format_datetime(datetime.fromisoformat(clock.replace('Z', '+00:00')), usegmt=True)
        self.ordered = step.get('ordered', False)
        self.calls = []
        self.rejected = []
        self.traces = [self.compile_trace(data, wire)
                       for wire in [step['wire'], *step.get('wire_alternatives', [])]]
        self.credentials = {value for mode in data['modes'].values()
                            for name, value in mode.items() if name != 'api'}
        self.credentials.update(value.removeprefix('Bearer ')
                                for query, headers in data['auth'].values()
                                for value in (*query.values(), *headers.values()))

    @staticmethod
    def compile_trace(data, wire_steps):
        pending = []
        for route, auth, payload in wire_steps:
            path, query, wire = data['routes'][route]
            auth_query, auth_headers = data['auth'][auth]
            if set(query).intersection(auth_query):
                raise ValueError(f'overlapping query parameters on {route}')
            signature = ('GET', path, tuple(sorted((query | auth_query).items())),
                         tuple(sorted(auth_headers.items())))
            pending.append((signature, route, auth, reply(data, payload, wire)))
        return pending

    def take(self, method, target, headers):
        with self.lock:
            call = {'method': method, 'target': target, 'headers': {
                name: headers.get_all(name) for name in WATCHED_HEADERS if name in headers}}
            self.calls.append(call)
            try:
                url = urlsplit(target)
                query = tuple(sorted(parse_qsl(url.query, keep_blank_values=True, strict_parsing=True)))
                observed = tuple(sorted((name, value) for name, values in call['headers'].items()
                                        for value in values))
                if url.scheme or url.netloc or url.fragment or not url.path.startswith('/'):
                    raise ValueError('expected an origin-form target')
                if headers.get_all('Transfer-Encoding') or headers.get_all('Content-Length', []) not in ([], ['0']):
                    raise ValueError('expected a bodyless read')
                misplaced = [name for name, value in headers.items()
                             if name.lower() not in WATCHED_HEADERS
                             and any(credential in value for credential in self.credentials)]
                if misplaced:
                    call['misplaced_credential_headers'] = misplaced
                    raise ValueError('credential outside its declared header')
                signature = (method, url.path, query, observed)
                matches = []
                replies = []
                for pending in self.traces:
                    candidates = pending[:1] if self.ordered else pending
                    for index, item in enumerate(candidates):
                        if signature == item[0]:
                            matches.append(pending[:index] + pending[index + 1:])
                            replies.append(item[1:])
                            break
                if matches:
                    route, auth, response = replies[0]
                    if any(option != replies[0] for option in replies[1:]):
                        raise RuntimeError('ambiguous fixture responses for the same request')
                    self.traces = matches
                    call.update(route=route, credential=auth, status=response[0])
                    return response
            except ValueError:
                pass
            self.rejected.append(call)
            call['status'] = 405 if method != 'GET' else 400
            return call['status'], {}, 0, b'{"error":"unexpected fixture request"}'

    def report(self):
        with self.lock:
            closest = min(self.traces, key=len)
            missing = Counter((item[1], item[2]) for item in closest)
            detail = {'missing': [[route, auth, count] for (route, auth), count in missing.items()],
                      'rejected': list(self.rejected)}
            return list(self.calls), detail if missing or self.rejected else None


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def __getattr__(self, name):
        if name.startswith('do_'):
            return self.handle_contract
        raise AttributeError(name)

    def handle_contract(self):
        plan = self.server.plan
        if plan is None:
            self.send_error(503)
            return
        status, headers, delay, body = plan.take(self.command, self.path, self.headers)
        if delay:
            sleep(delay)
        try:
            self.send_response_only(status)
            self.send_header('Date', plan.date)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Connection', 'close')
            for key, value in headers.items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            # The timeout scenario intentionally closes the client socket first.
            pass


@contextmanager
def serve():
    with ThreadingHTTPServer(('127.0.0.1', 0), Handler) as server:
        server.plan = None
        thread = Thread(target=server.serve_forever, kwargs={'poll_interval': 0.05}, daemon=True)
        thread.start()
        try:
            yield server, f'http://127.0.0.1:{server.server_port}'
        finally:
            server.shutdown()
            thread.join()
