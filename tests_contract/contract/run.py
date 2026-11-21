"""Run a fixture-backed observer and compare only its public observations.

Run as `python contract/run.py good`. An optional `--observer module:factory`
loads a zero-argument factory. It returns an object with observe(url, request)
and optionally close(). The runner replaces the observer on fresh setup,
reload, upgrade and restart. An observer with durable state implements
dump_state() and load_state(state), using JSON-compatible data. On a same-store
transition only the serialized durable state passes to the replacement.
For a seeded start load_state receives {'registry': [...]} as fixture input.
The request describes mode, credentials, clock, operation, parameters,
lifecycle and synthetic registry configuration. Expected observations, captured
registry assertions and reply plans are private to the runner.

Return a mapping of observed fields. Sensor records are five-element lists
in the order in expected.json. Unknown and unavailable use a null value and
time. The optional HTTP status attribute on protected failures is normalized
as an integer. Calendar values are chronological event lists in the documented
field order. The registry observation is a list of all rows for the four
integration roles, including any duplicates. Each row exposes role, domain,
platform, unique_id, id, entity_id, name and aliases. A real adapter may
normalize HA's labels and units at this boundary. Loopback credentials are
synthetic.

For steps with registry queries, implement lookup_registry(domain, platform,
unique_id) on the same observer. After observe(), the runner issues each tuple
as a separate query. Resolve it against the observer's current registry index
and return the opaque row id string or None when absent. This call is local
and makes no HTTP requests. The lookup results are compared independently of
registry enumeration. Generation 2 migrates the roles in registry_migration_roles
when supplied and all registry roles by default.

Exit codes are 0 for agreement, 1 for a behavior mismatch and 2 for setup or
harness failure. The author left the suite unrun.
"""

import argparse
import importlib
import json
import math
import sys
from pathlib import Path

if __package__:
    from .controls import Control
    from .service import Plan, serve
else:
    from controls import Control
    from service import Plan, serve


class Mismatch(Exception):
    pass


def read_json(path):
    with path.open(encoding='utf-8') as stream:
        return json.load(stream)


def requests_for(data, case, steps):
    if len(case['steps']) != len(steps) or not steps:
        raise ValueError(f'{case["id"]} has inconsistent step counts')
    for index, (step, groups) in enumerate(zip(case['steps'], steps), 1):
        mode = step.get('mode', case['mode'])
        clock = step.get('clock', case.get('clock', data['clock']))
        parameters = {name: values | case.get('parameters', {}).get(name, {})
                            | step.get('parameters', {}).get(name, {})
                      for name, values in data['parameters'].items()}
        request = dict(operation=case['operation'], mode=mode, clock=clock,
                       credentials=data['modes'][mode], parameters=parameters,
                       lifecycle=step.get('lifecycle', 'poll'),
                       units=step.get('units', case.get('units', 'mg/dL')),
                       maintenance=case.get('maintenance'),
                       registry_site=step.get('registry_site', 'fixture-site'),
                       registry_store=step.get('registry_store', case['id']),
                       registry_generation=step.get('registry_generation', 1))
        if 'registry_migration_roles' in case:
            request['registry_migration_roles'] = case['registry_migration_roles']
        yield index, step, request, groups


def preflight(data, oracle):
    ids = [case['id'] for case in data['cases']]
    if not ids or len(ids) != len(set(ids)) or set(ids) != set(oracle['cases']):
        raise ValueError('case IDs and expectation IDs must match one-to-one')
    if not data['ruling_checks'] or set(data['ruling_checks']) != set(oracle['downstream']):
        raise ValueError('ruling links and downstream obligations must match one-to-one')
    prepared = {}
    for case in data['cases']:
        phases = []
        for index, step, request, groups in requests_for(data, case, oracle['cases'][case['id']]):
            expected = {}
            for name in groups:
                group = oracle['groups'][name]
                overlap = expected.keys() & group['observe'].keys()
                if overlap or not group['basis']:
                    raise ValueError(f'{case["id"]} step {index} has inconsistent group {name}')
                expected.update(group['observe'])
            if not expected or not step['wire']:
                raise ValueError(f'{case["id"]} step {index} has an empty plan')
            seed = data['registry_seeds'][step['registry_seed']] if 'registry_seed' in step else None
            if seed is not None and request['lifecycle'] != 'fresh':
                raise ValueError('a registry seed requires a fresh observer')
            lookups = (data['registry_lookup_queries'][step['registry_lookup']]
                       if 'registry_lookup' in step else None)
            if ('registry_lookup' in expected) != (lookups is not None):
                raise ValueError(f'{case["id"]} step {index} requires both lookup queries and expectations')
            if lookups is not None:
                results = expected['registry_lookup']
                if (request['operation'] != 'identity' or not isinstance(lookups, list) or not lookups
                        or any(not isinstance(query, list) or len(query) != len(REGISTRY_LOOKUP)
                               or any(not isinstance(part, str) or not part for part in query)
                               for query in lookups)
                        or not isinstance(results, list) or len(results) != len(lookups)
                        or any(value is not None and (not isinstance(value, str) or not value)
                               for value in results)):
                    raise ValueError(f'{case["id"]} step {index} has invalid registry lookup data')
            phases.append((index, request, expected, Plan(data, step, request['clock']),
                           seed, step.get('empty_store', False), lookups))
        prepared[case['id']] = phases
    for ruling, check in data['ruling_checks'].items():
        if not check['cases'] or not check['fields']:
            raise ValueError(f'{ruling} has no executable assertions')
        case_fields = check.get('case_fields', {})
        if not case_fields.keys() <= check['cases'].keys():
            raise ValueError(f'{ruling} has assertions for an unlinked case')
        for case_id, indices in check['cases'].items():
            fields = set(check['fields']).union(case_fields.get(case_id, ()))
            if not indices:
                raise ValueError(f'{ruling} has no steps for {case_id}')
            for index in indices:
                if not 1 <= index <= len(prepared[case_id]):
                    raise ValueError(f'{ruling} identifies a nonexistent step in {case_id}')
                if not fields.issubset(prepared[case_id][index - 1][2]):
                    raise ValueError(f'{ruling} identifies unasserted fields in {case_id} step {index}')
    return prepared


REGISTRY_ROLES = {'glucose': 'sensor', 'latest_bolus': 'sensor',
                  'latest_carbs': 'sensor', 'calendar': 'calendar'}
REGISTRY_LOOKUP = ('domain', 'platform', 'unique_id')
REGISTRY_STRINGS = (*REGISTRY_LOOKUP, 'id', 'entity_id')
REGISTRY_FIELDS = {*REGISTRY_STRINGS, 'role', 'name', 'aliases'}


def registry_rows(actual, path):
    if not isinstance(actual, list) or len(actual) != len(REGISTRY_ROLES):
        raise Mismatch(f'{path} expected exactly one stored row per logical role')
    rows = {}
    for row in actual:
        if not isinstance(row, dict) or row.keys() != REGISTRY_FIELDS:
            raise Mismatch(f'{path} invalid stored row {row!r}')
        role = row['role']
        if (not isinstance(role, str) or role not in REGISTRY_ROLES or role in rows
                or any(not isinstance(row[key], str) or not row[key].strip() for key in REGISTRY_STRINGS)
                or row['domain'] != REGISTRY_ROLES[role]
                or not row['entity_id'].startswith(row['domain'] + '.')
                or (row['name'] is not None and not isinstance(row['name'], str))
                or not isinstance(row['aliases'], list)
                or any(not isinstance(alias, str) for alias in row['aliases'])):
            raise Mismatch(f'{path} invalid or duplicate role row {row!r}')
        rows[role] = row
    for keys in (REGISTRY_LOOKUP, ('id',), ('entity_id',)):
        if len({tuple(row[key] for key in keys) for row in rows.values()}) != len(rows):
            raise Mismatch(f'{path} shared registry identity for {keys!r}')
    return rows


def compare_registry(rule, actual, captures, path):
    rows = registry_rows(actual, path)
    if rule == 'seed':
        compare(registry_rows(captures['registry_seed'], 'fixture seed'), rows, captures, path)
    elif rule == 'fresh':
        for role, previous in captures['registry'].items():
            for key in REGISTRY_LOOKUP:
                compare(previous[key], rows[role][key], captures, f'{path}.{role}.{key}')
    elif isinstance(rule, dict) and rule.keys() == {'migrate'}:
        migrated = dict(captures['registry'])
        for role in rule['migrate']:
            previous = migrated[role]
            migrated[role] = previous | {'unique_id': previous['unique_id'] + '/v2'}
        compare(migrated, rows, captures, path)
    elif rule == 'same':
        compare(captures['registry'], rows, captures, path)
    elif rule != 'capture':
        raise ValueError(f'unknown registry assertion {rule}')
    captures['registry'] = json.loads(json.dumps(rows))


def compare(expected, actual, captures, path='observation'):
    if isinstance(expected, dict):
        if set(expected) == {'$registry'}:
            compare_registry(expected['$registry'], actual, captures, path)
            return
        if set(expected) == {'$near', 'absolute'}:
            if (type(actual) not in (int, float) or not math.isfinite(actual)
                    or abs(actual - expected['$near']) > expected['absolute']):
                raise Mismatch(f'{path} expected about {expected["$near"]}, got {actual!r}')
            return
        if not isinstance(actual, dict) or actual.keys() != expected.keys():
            raise Mismatch(f'{path} expected fields {list(expected)}, got {actual!r}')
        for key, value in expected.items():
            compare(value, actual[key], captures, f'{path}.{key}')
        return
    if isinstance(expected, list):
        if not isinstance(actual, list) or len(actual) != len(expected):
            raise Mismatch(f'{path} expected {expected!r}, got {actual!r}')
        for index, (want, got) in enumerate(zip(expected, actual)):
            compare(want, got, captures, f'{path}[{index}]')
        return
    if expected != actual or isinstance(expected, bool) != isinstance(actual, bool):
        raise Mismatch(f'{path} expected {expected!r}, got {actual!r}')


def factory(spec, mode):
    if spec is None:
        return lambda: Control(mode)
    if mode != 'good':
        raise ValueError('--observer is used with good mode only')
    module, name = spec.split(':', 1)
    target = getattr(importlib.import_module(module), name)
    if not callable(target):
        raise ValueError('--observer must identify a callable factory')
    return target


def close_observer(observer):
    close = getattr(observer, 'close', None)
    if close is not None:
        close()


def execute(prepared, selected, make_observer, audit):
    with serve() as (server, url):
        for case_id in selected:
            observer = None
            captures = {}
            try:
                for index, request, expected, plan, seed, empty_store, lookups in prepared[case_id]:
                    label = f'{case_id} step {index} ({request["operation"]})'
                    lifecycle = request['lifecycle']
                    server.plan = None
                    if observer is None or lifecycle != 'poll':
                        persisted = None
                        if seed is not None:
                            captures['registry_seed'] = seed
                            persisted = json.dumps({'registry': seed}, allow_nan=False)
                        elif observer is not None and lifecycle != 'fresh' and not empty_store:
                            dump = getattr(observer, 'dump_state', None)
                            if dump is not None:
                                persisted = json.dumps(dump(), allow_nan=False)
                            elif request['operation'] == 'identity':
                                raise ValueError(f'{label}: registry lifecycle requires dump_state()')
                        retiring, observer = observer, None
                        close_observer(retiring)
                        observer = make_observer()
                        if persisted is not None:
                            load = getattr(observer, 'load_state', None)
                            if load is None:
                                raise ValueError(f'{label}: persisted lifecycle requires load_state(state)')
                            load(json.loads(persisted))
                    server.plan = plan
                    actual = observer.observe(url, request)
                    calls, failure = plan.report()
                    if audit:
                        print(json.dumps({'case': case_id, 'step': index, 'requests': calls}), flush=True)
                    try:
                        if failure:
                            raise Mismatch(f'wire {json.dumps(failure, sort_keys=True)}')
                        observations = {key: value for key, value in expected.items()
                                        if key != 'registry_lookup'}
                        compare(observations, actual, captures)
                        if lookups is not None:
                            lookup = getattr(observer, 'lookup_registry', None)
                            if not callable(lookup):
                                raise ValueError(f'{label}: registry queries require lookup_registry()')
                            for query, want in zip(lookups, expected['registry_lookup']):
                                compare(want, lookup(*query), captures, f'registry_lookup{tuple(query)!r}')
                            if len(plan.report()[0]) != len(calls):
                                raise Mismatch('registry lookup made a fixture HTTP request')
                    except Mismatch as error:
                        raise Mismatch(f'{label}: {error}') from error
            finally:
                close_observer(observer)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('mode', nargs='?', default='good',
                        choices=('good', 'wrong-route', 'wrong-state', 'broken-environment'))
    parser.add_argument('--observer', metavar='MODULE:FACTORY')
    parser.add_argument('--case', action='append', help='run a selected case ID, repeatable')
    parser.add_argument('--audit', action='store_true', help='print actual synthetic credential destinations')
    args = parser.parse_args(argv)
    root = Path(__file__).resolve().parent
    try:
        case_path = root / 'cases.json'
        if args.mode == 'broken-environment':
            case_path = Path(__file__).resolve() / 'cases.json'
        data = read_json(case_path)
        oracle = read_json(root / 'expected.json')
        prepared = preflight(data, oracle)
        selected = args.case or list(prepared)
        if any(case_id not in prepared for case_id in selected):
            raise ValueError('unknown --case ID')
        execute(prepared, selected, factory(args.observer, args.mode), args.audit)
    except Mismatch as error:
        print(f'FAIL {error}', file=sys.stderr)
        return 1
    except Exception as error:
        print(f'HARNESS {type(error).__name__}: {error}', file=sys.stderr)
        return 2
    print(f'PASS {len(selected)} cases, {sum(len(prepared[key]) for key in selected)} observation steps.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
