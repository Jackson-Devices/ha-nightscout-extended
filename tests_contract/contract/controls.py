"""Replaceable synthetic observer controls. No fixture or oracle imports."""

import http.client
import json
import math
from datetime import datetime, timedelta, timezone
from statistics import mean
from urllib.parse import quote, urlencode, urlsplit
from uuid import NAMESPACE_URL, uuid5


MGDL_PER_MMOL = 18.01559
REGISTRY_DOMAINS = {'glucose': 'sensor', 'latest_bolus': 'sensor',
                    'latest_carbs': 'sensor', 'calendar': 'calendar'}


class ReadFailure(Exception):
    def __init__(self, status=None):
        super().__init__(status)
        self.status = status


def stamp(value):
    try:
        if type(value) in (int, float):
            return datetime.fromtimestamp(value / 1000, tz=timezone.utc)
        if isinstance(value, str):
            result = datetime.fromisoformat(value.replace('Z', '+00:00'))
            if result.tzinfo is not None:
                return result.astimezone(timezone.utc)
    except (ValueError, OverflowError, OSError):
        pass
    return None


def iso(value):
    return value.isoformat().replace('+00:00', 'Z') if value is not None else None


def numeric(value):
    return type(value) in (int, float) and math.isfinite(value)


def record(value, unit=None, time=None, availability=None, **attributes):
    return [value, unit, iso(time), availability or ('unknown' if value is None else 'available'), attributes]


def unavailable(unit=None, failure=None):
    attributes = {'http_status': failure.status} if failure and failure.status in (401, 403) else {}
    return record(None, unit, availability='unavailable', **attributes)


def field(document, path):
    for component in path.split('.'):
        if not isinstance(document, dict):
            return None
        document = document.get(component)
    return document


def collection(payload):
    if isinstance(payload, list) and all(isinstance(row, dict) for row in payload):
        return payload
    raise ReadFailure()


def source_id(row):
    return row.get('identifier', row.get('_id'))


def event_time(row):
    return stamp(row.get('date', row.get('created_at')))


def display_unit(value):
    if value in ('mmol', 'mmol/l', 'mmol/L'):
        return 'mmol/L'
    if value in ('mg', 'mg/dl', 'mg/dL'):
        return 'mg/dL'
    return None


class Control:
    def __init__(self, mutation='good'):
        self.mutation = mutation
        self.mode = None
        self.bearer = None
        self.bearer_expires = None
        self.registry = []
        self.registry_index = {}

    def dump_state(self):
        return {'registry': self.registry}

    def load_state(self, state):
        self.registry = state['registry']
        self.registry_index = {(row['domain'], row['platform'], row['unique_id']): row
                               for row in self.registry}

    def lookup_registry(self, domain, platform, unique_id):
        row = self.registry_index.get((domain, platform, unique_id))
        return row['id'] if row is not None else None

    def observe(self, url, request):
        self.origin = urlsplit(url)
        if self.origin.scheme != 'http' or self.origin.hostname != '127.0.0.1':
            raise ValueError('the synthetic control requires its loopback fixture')
        self.request = request
        self.now = stamp(request['clock'])
        self.credentials = request['credentials']
        if self.mode != request['mode']:
            self.bearer = self.credentials.get('initial_bearer')
            self.bearer_expires = None
        self.mode = request['mode']
        operation = request['operation']
        methods = {'version': self.version, 'glucose': self.glucose, 'site': self.site,
                   'derived': self.derived, 'forecast': self.forecast, 'treatments': self.treatments,
                   'maintenance': self.maintenance, 'reservoir': self.reservoir,
                   'recovery': self.recovery, 'identity': self.identity}
        result = methods[operation]()
        if self.mutation == 'wrong-state' and 'glucose' in result and numeric(result['glucose'][0]):
            result['glucose'][0] += 1
        return result

    @property
    def v3(self):
        return self.credentials['api'] == 'v3'

    def get(self, path, query=None, legacy=False, bearer=None):
        query = dict(query or {})
        headers = {'Accept': 'application/json'}
        if legacy:
            if self.credentials.get('api_secret_hash'):
                headers['api-secret'] = self.credentials['api_secret_hash']
            if self.credentials.get('query_token'):
                query['token'] = self.credentials['query_token']
            bearer = self.credentials.get('legacy_bearer')
        if bearer:
            headers['Authorization'] = 'Bearer ' + bearer
        if query:
            path += '?' + urlencode(query)
        connection = http.client.HTTPConnection(self.origin.hostname, self.origin.port, timeout=0.55)
        try:
            connection.request('GET', path, headers=headers)
            with connection.getresponse() as response:
                if response.status != 200:
                    raise ReadFailure(response.status)
                return json.loads(response.read())
        except (TimeoutError, UnicodeError, json.JSONDecodeError, http.client.IncompleteRead) as error:
            raise ReadFailure() from error
        finally:
            connection.close()

    def exchange(self):
        subject = quote(self.credentials['subject_token'], safe='')
        payload = self.get('/api/v2/authorization/request/' + subject)
        token, subject_name = field(payload, 'token'), field(payload, 'sub')
        groups = field(payload, 'permissionGroups')
        issued, expires = field(payload, 'iat'), field(payload, 'exp')
        if (not isinstance(token, str) or not token or not isinstance(subject_name, str) or not subject_name
                or not isinstance(groups, list) or any(not isinstance(group, str) for group in groups)
                or not numeric(issued) or not numeric(expires) or not issued <= self.now.timestamp() < expires):
            raise ReadFailure()
        self.bearer = token
        self.bearer_expires = expires

    def protected(self, path, query):
        can_exchange = bool(self.credentials.get('subject_token'))
        if can_exchange and (self.bearer is None or (self.bearer_expires is not None
                                                     and self.now.timestamp() >= self.bearer_expires)):
            self.exchange()
        try:
            return field(self.get(path, query, bearer=self.bearer), 'result')
        except ReadFailure as error:
            if error.status != 401 or not can_exchange:
                raise
        rejected = self.bearer
        self.bearer = None
        self.bearer_expires = None
        self.exchange()
        if self.bearer == rejected:
            self.bearer = None
            self.bearer_expires = None
            raise ReadFailure(401)
        try:
            return field(self.get(path, query, bearer=self.bearer), 'result')
        except ReadFailure as error:
            if error.status == 401:
                self.bearer = None
                self.bearer_expires = None
            raise

    def search(self, kind, limit):
        if self.v3:
            return collection(self.protected('/api/v3/' + kind, {'limit': limit}))
        path = '/api/v1/' + kind
        if kind == 'devicestatus':
            path += '/'
        if kind == 'entries' and self.mutation == 'wrong-route':
            path += '/sgv'
        return collection(self.get(path, {'count': limit}, legacy=True))

    def version(self):
        payload = field(self.get('/api/v3/version'), 'result')
        return {'api_version': record(payload['apiVersion'], time=stamp(payload.get('srvDate')))}

    def glucose_history(self):
        candidates = []
        seen = set()
        for row in self.search('entries', 8):
            time = stamp(row.get('date', row.get('dateString')))
            if row.get('type') != 'sgv' or not numeric(row.get('sgv')) or time is None or time > self.now:
                continue
            identity = source_id(row) or (time, row['sgv'], row.get('direction'))
            if identity not in seen:
                seen.add(identity)
                candidates.append((time, row))
        return sorted(candidates, key=lambda item: item[0], reverse=True)

    @staticmethod
    def mgdl(row):
        value = row['sgv']
        return value * MGDL_PER_MMOL if display_unit(row.get('units', 'mg')) == 'mmol/L' else value

    @staticmethod
    def round_display(value, digits=0):
        scale = 10 ** digits
        rounded = math.floor(value * scale + 0.5)
        return rounded if digits == 0 else rounded / scale

    def derived_delta(self, history, unit):
        anchor = history[0][0]
        buckets = [[], [], [], []]
        for time, row in history:
            lag = (anchor - time).total_seconds()
            bucket = int((lag + 150) // 300)
            if bucket < len(buckets):
                buckets[bucket].append((lag, self.mgdl(row)))
        older = next((bucket for bucket in buckets[1:] if bucket), None)
        if older is None:
            return None
        current = mean(value for _, value in buckets[0])
        previous = mean(value for _, value in older)
        elapsed = (mean(lag for lag, _ in older) - mean(lag for lag, _ in buckets[0])) / 60
        if elapsed > 9:
            previous = current - (current - previous) * 5 / elapsed
        if unit == 'mmol/L':
            now_scaled = self.round_display(current / MGDL_PER_MMOL, 1)
            prev_scaled = self.round_display(previous / MGDL_PER_MMOL, 1)
            return self.round_display(now_scaled - prev_scaled, 1)
        return self.round_display(current - previous)

    def glucose(self):
        unit = self.request['units']
        try:
            history = self.glucose_history()
            if not history:
                return {'glucose': record(None, unit)}
            if self.request['parameters']['glucose']['delta_source'] == 'properties':
                properties = self.get('/api/v2/properties/bgnow,delta', legacy=True)
                delta = field(properties, 'delta.scaled' if unit == 'mmol/L' else 'delta.mgdl')
            else:
                delta = self.derived_delta(history, unit)
        except ReadFailure as error:
            return {'glucose': unavailable(unit, error)}
        time, row = history[0]
        value = self.mgdl(row)
        if display_unit(row.get('units', 'mg')) != unit:
            value = (self.round_display(value / MGDL_PER_MMOL, 1) if unit == 'mmol/L'
                     else self.round_display(value))
        attributes = {'trend': row.get('direction')}
        if numeric(delta):
            attributes['delta'] = delta
        return {'glucose': record(value, unit, time, **attributes)}

    def site(self):
        try:
            settings = field(self.get('/api/v1/status', legacy=True), 'settings')
        except ReadFailure as error:
            return {'site_settings': unavailable(failure=error)}
        if not isinstance(settings, dict):
            return {'site_settings': record(None)}
        value = {'display_unit': display_unit(settings.get('units')),
                 'threshold_unit': 'mg/dL', 'thresholds': settings.get('thresholds', {})}
        return {'site_settings': record(value)}

    def derived(self):
        try:
            properties = self.get('/api/v2/properties/iob,cob,basal', legacy=True)
        except ReadFailure:
            properties = None
        # Exercise the independent uploader read without treating it as a calculation.
        try:
            self.search('devicestatus', 12)
        except ReadFailure:
            pass
        result = self.site()
        for name, unit in (('iob', 'U'), ('cob', 'g'), ('basal', 'U/h')):
            path = self.request['parameters']['calculated_properties'][name]
            value = field(properties, path)
            time = stamp(field(properties, f'{name}.mills')) if name in ('iob', 'cob') else None
            if not isinstance(properties, dict):
                result[name] = unavailable(unit)
            else:
                result[name] = record(value, unit, time) if numeric(value) else record(None, unit)
        return result

    def forecast(self):
        try:
            history = self.glucose_history()
            anchor = history[0][0] if history else None
            raw = self.get('/api/v2/properties/ar2', legacy=True)
        except ReadFailure:
            return {f'forecast_{minutes}': unavailable('mg/dL') for minutes in (15, 30)}
        try:
            self.search('devicestatus', 12)
        except ReadFailure:
            pass
        points = field(raw, 'ar2.forecast.predicted')
        timed = [(stamp(point.get('mills')), point['mgdl']) for point in points
                 if isinstance(point, dict) and numeric(point.get('mgdl'))] if isinstance(points, list) else []
        result = {}
        for minutes in (15, 30):
            name = f'forecast_{minutes}'
            result[name] = record(None, 'mg/dL')
            if anchor is None:
                continue
            nominal = anchor + timedelta(minutes=minutes)
            native = nominal + timedelta(milliseconds=2000)
            values = {value for time, value in timed if time in (nominal, native)}
            if len(values) == 1:
                result[name] = record(next(iter(values)), 'mg/dL', nominal)
        return result

    def read_calendar(self):
        window = self.request['parameters']['calendar']
        start = window.get('source_start', window['start'])
        end = window.get('source_end', window['end'])
        if not self.v3:
            query = {'count': 50, 'find[created_at][$gte]': start, 'find[created_at][$lt]': end}
            return collection(self.get('/api/v1/treatments', query, legacy=True))
        size = window['page_size']
        rows = []
        for skip in range(0, 100 * size, size):
            query = {'limit': size, 'skip': skip, 'sort$desc': 'date', 'date$gte': start, 'date$lt': end}
            page = collection(self.protected('/api/v3/treatments', query))
            rows.extend(page)
            if len(page) < size:
                return rows
        raise ReadFailure()

    def calendar(self):
        try:
            rows = self.read_calendar()
        except ReadFailure as error:
            return unavailable(failure=error)
        window = self.request['parameters']['calendar']
        start, end = stamp(window['start']), stamp(window['end'])
        events = {}
        for row in rows:
            when, identifier = event_time(row), source_id(row)
            if not identifier or when is None:
                continue
            duration = row.get('duration')
            until = when + timedelta(minutes=duration) if numeric(duration) and duration > 0 else None
            overlaps = until > start and when < end if until is not None else start <= when < end
            if not overlaps:
                continue
            insulin, carbs = row.get('insulin'), row.get('carbs')
            events[identifier] = [identifier, iso(when), row.get('eventType'),
                                  insulin if numeric(insulin) else None,
                                  carbs if numeric(carbs) else None, row.get('notes'), iso(until)]
        return record(sorted(events.values(), key=lambda event: (event[1], event[0])))

    def treatments(self):
        result = {}
        fields = [('latest_bolus', 'insulin', 'U'), ('latest_carbs', 'carbs', 'g')]
        try:
            rows = self.search('treatments', 50)
        except ReadFailure as error:
            for name, _, unit in fields:
                result[name] = unavailable(unit, error)
        else:
            dated = [(event_time(row), row) for row in rows]
            for name, key, unit in fields:
                candidates = [(time, row) for time, row in dated
                              if time is not None and time <= self.now and numeric(row.get(key))]
                time, row = max(candidates, key=lambda item: item[0], default=(None, {}))
                result[name] = (record(row[key], unit, time, source_id=source_id(row))
                                if row else record(None, unit))
        result['calendar'] = self.calendar()
        return result

    def maintenance(self):
        match = {'insulin_age': ('Insulin Change',), 'site_age': ('Site Change',),
                 'sensor_age': ('Sensor Start', 'Sensor Change'), 'battery_age': ('Pump Battery Change',)}
        names = self.request['maintenance']
        if isinstance(names, str):
            names = [names]
        try:
            limit = 50
            rows = self.search('treatments', limit)
            if len(rows) >= limit:
                raise ReadFailure()
        except ReadFailure as error:
            return {name: unavailable('h', error) for name in names}
        result = {}
        for name in names:
            candidates = []
            for row in rows:
                time, kind = event_time(row), row.get('eventType')
                if kind in match[name] and time is not None and time <= self.now:
                    candidates.append((time, kind == 'Sensor Change', kind))
            if not candidates:
                result[name] = record(None, 'h')
                continue
            newest, _, kind = max(candidates)
            hours = int((self.now - newest).total_seconds() // 3600)
            result[name] = record(hours, 'h', newest, event_type=kind)
        return result

    def reservoir(self):
        unit = 'U'
        name = 'insulin_remaining'
        config = self.request['parameters']['reservoir']
        missing = {name: unavailable(unit)}
        try:
            rows = [row for row in self.search('devicestatus', 12) if isinstance(row.get('pump'), dict)]
        except ReadFailure as error:
            return {name: unavailable(unit, error)}
        identity = config['identity']
        if identity is None:
            return missing
        candidates = []
        for row in rows:
            if field(row, config['identity_field']) != identity:
                continue
            measurement = stamp(field(row, config['measurement_field']))
            if measurement is not None and measurement <= self.now:
                candidates.append((measurement, field(row, config['value_field'])))
        if not candidates:
            return missing
        newest = max(time for time, _ in candidates)
        age = (self.now - newest).total_seconds()
        cutoff = config['max_age_seconds']
        if age > cutoff or (age == cutoff and not config['inclusive']):
            return missing
        values = [value for time, value in candidates if time == newest]
        if any(not numeric(value) or value < 0 for value in values) or len(set(values)) != 1:
            return missing
        return {name: record(values[0], unit, newest)}

    def recovery(self):
        return self.glucose() | self.site()

    def identity(self):
        observations = self.glucose() | self.treatments() | self.site()
        site = self.request['registry_site']
        platform = 'nightscout'
        generation = self.request['registry_generation']
        migration_roles = self.request.get('registry_migration_roles', REGISTRY_DOMAINS)
        index = self.registry_index
        for role, domain in REGISTRY_DOMAINS.items():
            legacy_key = f'{site}/{role}'
            migrate = generation == 2 and role in migration_roles
            unique_id = legacy_key + '/v2' if migrate else legacy_key
            lookup = (domain, platform, unique_id)
            if lookup in index:
                continue
            legacy_lookup = (domain, platform, legacy_key)
            if migrate and legacy_lookup in index:
                row = index.pop(legacy_lookup)
                row['unique_id'] = unique_id
            else:
                store = self.request['registry_store']
                row = {'role': role, 'domain': domain, 'platform': platform, 'unique_id': unique_id,
                       'id': uuid5(NAMESPACE_URL, f'{store}/{site}/{role}').hex,
                       'entity_id': f'{domain}.{site.replace("-", "_")}_{role}',
                       'name': None, 'aliases': []}
                self.registry.append(row)
            index[lookup] = row
        observations['registry'] = self.registry
        return observations
