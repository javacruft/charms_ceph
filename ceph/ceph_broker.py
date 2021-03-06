#!/usr/bin/python
#
# Copyright 2016 Canonical Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#  http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import json
import os
from tempfile import NamedTemporaryFile

from charmhelpers.core.hookenv import (
    log,
    DEBUG,
    INFO,
    ERROR,
)
from charmhelpers.contrib.storage.linux.ceph import (
    create_erasure_profile,
    delete_pool,
    erasure_profile_exists,
    get_osds,
    pool_exists,
    pool_set,
    remove_pool_snapshot,
    rename_pool,
    set_pool_quota,
    snapshot_pool,
    validator,
    ErasurePool,
    Pool,
    ReplicatedPool,
)

# This comes from http://docs.ceph.com/docs/master/rados/operations/pools/
# This should do a decent job of preventing people from passing in bad values.
# It will give a useful error message
from subprocess import check_output, CalledProcessError

POOL_KEYS = {
    # "Ceph Key Name": [Python type, [Valid Range]]
    "size": [int],
    "min_size": [int],
    "crash_replay_interval": [int],
    "pgp_num": [int],  # = or < pg_num
    "crush_ruleset": [int],
    "hashpspool": [bool],
    "nodelete": [bool],
    "nopgchange": [bool],
    "nosizechange": [bool],
    "write_fadvise_dontneed": [bool],
    "noscrub": [bool],
    "nodeep-scrub": [bool],
    "hit_set_type": [str, ["bloom", "explicit_hash",
                           "explicit_object"]],
    "hit_set_count": [int, [1, 1]],
    "hit_set_period": [int],
    "hit_set_fpp": [float, [0.0, 1.0]],
    "cache_target_dirty_ratio": [float],
    "cache_target_dirty_high_ratio": [float],
    "cache_target_full_ratio": [float],
    "target_max_bytes": [int],
    "target_max_objects": [int],
    "cache_min_flush_age": [int],
    "cache_min_evict_age": [int],
    "fast_read": [bool],
}

CEPH_BUCKET_TYPES = [
    'osd',
    'host',
    'chassis',
    'rack',
    'row',
    'pdu',
    'pod',
    'room',
    'datacenter',
    'region',
    'root'
]


def decode_req_encode_rsp(f):
    """Decorator to decode incoming requests and encode responses."""

    def decode_inner(req):
        return json.dumps(f(json.loads(req)))

    return decode_inner


@decode_req_encode_rsp
def process_requests(reqs):
    """Process Ceph broker request(s).

    This is a versioned api. API version must be supplied by the client making
    the request.
    """
    request_id = reqs.get('request-id')
    try:
        version = reqs.get('api-version')
        if version == 1:
            log('Processing request {}'.format(request_id), level=DEBUG)
            resp = process_requests_v1(reqs['ops'])
            if request_id:
                resp['request-id'] = request_id

            return resp

    except Exception as exc:
        log(str(exc), level=ERROR)
        msg = ("Unexpected error occurred while processing requests: %s" %
               reqs)
        log(msg, level=ERROR)
        return {'exit-code': 1, 'stderr': msg}

    msg = ("Missing or invalid api version (%s)" % version)
    resp = {'exit-code': 1, 'stderr': msg}
    if request_id:
        resp['request-id'] = request_id

    return resp


def handle_create_erasure_profile(request, service):
    # "local" | "shec" or it defaults to "jerasure"
    erasure_type = request.get('erasure-type')
    # "host" | "rack" or it defaults to "host"  # Any valid Ceph bucket
    failure_domain = request.get('failure-domain')
    name = request.get('name')
    k = request.get('k')
    m = request.get('m')
    l = request.get('l')

    if failure_domain not in CEPH_BUCKET_TYPES:
        msg = "failure-domain must be one of {}".format(CEPH_BUCKET_TYPES)
        log(msg, level=ERROR)
        return {'exit-code': 1, 'stderr': msg}

    create_erasure_profile(service=service, erasure_plugin_name=erasure_type,
                           profile_name=name, failure_domain=failure_domain,
                           data_chunks=k, coding_chunks=m, locality=l)


def handle_erasure_pool(request, service):
    pool_name = request.get('name')
    erasure_profile = request.get('erasure-profile')
    quota = request.get('max-bytes')
    weight = request.get('weight')

    if erasure_profile is None:
        erasure_profile = "default-canonical"

    # Check for missing params
    if pool_name is None:
        msg = "Missing parameter. name is required for the pool"
        log(msg, level=ERROR)
        return {'exit-code': 1, 'stderr': msg}

    # TODO: Default to 3/2 erasure coding. I believe this requires min 5 osds
    if not erasure_profile_exists(service=service, name=erasure_profile):
        # TODO: Fail and tell them to create the profile or default
        msg = ("erasure-profile {} does not exist.  Please create it with: "
               "create-erasure-profile".format(erasure_profile))
        log(msg, level=ERROR)
        return {'exit-code': 1, 'stderr': msg}

    pool = ErasurePool(service=service, name=pool_name,
                       erasure_code_profile=erasure_profile,
                       percent_data=weight)
    # Ok make the erasure pool
    if not pool_exists(service=service, name=pool_name):
        log("Creating pool '%s' (erasure_profile=%s)" % (pool.name,
                                                         erasure_profile),
            level=INFO)
        pool.create()

    # Set a quota if requested
    if quota is not None:
        set_pool_quota(service=service, pool_name=pool_name, max_bytes=quota)


def handle_replicated_pool(request, service):
    pool_name = request.get('name')
    replicas = request.get('replicas')
    quota = request.get('max-bytes')
    weight = request.get('weight')

    # Optional params
    pg_num = request.get('pg_num')
    if pg_num:
        # Cap pg_num to max allowed just in case.
        osds = get_osds(service)
        if osds:
            pg_num = min(pg_num, (len(osds) * 100 // replicas))

    # Check for missing params
    if pool_name is None or replicas is None:
        msg = "Missing parameter. name and replicas are required"
        log(msg, level=ERROR)
        return {'exit-code': 1, 'stderr': msg}

    kwargs = {}
    if pg_num:
        kwargs['pg_num'] = pg_num
    if weight:
        kwargs['percent_data'] = weight
    if replicas:
        kwargs['replicas'] = replicas

    pool = ReplicatedPool(service=service,
                          name=pool_name, **kwargs)
    if not pool_exists(service=service, name=pool_name):
        log("Creating pool '%s' (replicas=%s)" % (pool.name, replicas),
            level=INFO)
        pool.create()
    else:
        log("Pool '%s' already exists - skipping create" % pool.name,
            level=DEBUG)

    # Set a quota if requested
    if quota is not None:
        set_pool_quota(service=service, pool_name=pool_name, max_bytes=quota)


def handle_create_cache_tier(request, service):
    # mode = "writeback" | "readonly"
    storage_pool = request.get('cold-pool')
    cache_pool = request.get('hot-pool')
    cache_mode = request.get('mode')

    if cache_mode is None:
        cache_mode = "writeback"

    # cache and storage pool must exist first
    if not pool_exists(service=service, name=storage_pool) or not pool_exists(
            service=service, name=cache_pool):
        msg = ("cold-pool: {} and hot-pool: {} must exist. Please create "
               "them first".format(storage_pool, cache_pool))
        log(msg, level=ERROR)
        return {'exit-code': 1, 'stderr': msg}

    p = Pool(service=service, name=storage_pool)
    p.add_cache_tier(cache_pool=cache_pool, mode=cache_mode)


def handle_remove_cache_tier(request, service):
    storage_pool = request.get('cold-pool')
    cache_pool = request.get('hot-pool')
    # cache and storage pool must exist first
    if not pool_exists(service=service, name=storage_pool) or not pool_exists(
            service=service, name=cache_pool):
        msg = ("cold-pool: {} or hot-pool: {} doesn't exist. Not "
               "deleting cache tier".format(storage_pool, cache_pool))
        log(msg, level=ERROR)
        return {'exit-code': 1, 'stderr': msg}

    pool = Pool(name=storage_pool, service=service)
    pool.remove_cache_tier(cache_pool=cache_pool)


def handle_set_pool_value(request, service):
    # Set arbitrary pool values
    params = {'pool': request.get('name'),
              'key': request.get('key'),
              'value': request.get('value')}
    if params['key'] not in POOL_KEYS:
        msg = "Invalid key '%s'" % params['key']
        log(msg, level=ERROR)
        return {'exit-code': 1, 'stderr': msg}

    # Get the validation method
    validator_params = POOL_KEYS[params['key']]
    if len(validator_params) is 1:
        # Validate that what the user passed is actually legal per Ceph's rules
        validator(params['value'], validator_params[0])
    else:
        # Validate that what the user passed is actually legal per Ceph's rules
        validator(params['value'], validator_params[0], validator_params[1])

    # Set the value
    pool_set(service=service, pool_name=params['pool'], key=params['key'],
             value=params['value'])


def handle_rgw_regionmap_update(request, service):
    name = request.get('client-name')
    if not name:
        msg = "Missing rgw-region or client-name params"
        log(msg, level=ERROR)
        return {'exit-code': 1, 'stderr': msg}
    try:
        check_output(['radosgw-admin',
                      '--id', service,
                      'regionmap', 'update', '--name', name])
    except CalledProcessError as err:
        log(err.output, level=ERROR)
        return {'exit-code': 1, 'stderr': err.output}


def handle_rgw_regionmap_default(request, service):
    region = request.get('rgw-region')
    name = request.get('client-name')
    if not region or not name:
        msg = "Missing rgw-region or client-name params"
        log(msg, level=ERROR)
        return {'exit-code': 1, 'stderr': msg}
    try:
        check_output(
            [
                'radosgw-admin',
                '--id', service,
                'regionmap',
                'default',
                '--rgw-region', region,
                '--name', name])
    except CalledProcessError as err:
        log(err.output, level=ERROR)
        return {'exit-code': 1, 'stderr': err.output}


def handle_rgw_zone_set(request, service):
    json_file = request.get('zone-json')
    name = request.get('client-name')
    region_name = request.get('region-name')
    zone_name = request.get('zone-name')
    if not json_file or not name or not region_name or not zone_name:
        msg = "Missing json-file or client-name params"
        log(msg, level=ERROR)
        return {'exit-code': 1, 'stderr': msg}
    infile = NamedTemporaryFile(delete=False)
    with open(infile.name, 'w') as infile_handle:
        infile_handle.write(json_file)
    try:
        check_output(
            [
                'radosgw-admin',
                '--id', service,
                'zone',
                'set',
                '--rgw-zone', zone_name,
                '--infile', infile.name,
                '--name', name,
            ]
        )
    except CalledProcessError as err:
        log(err.output, level=ERROR)
        return {'exit-code': 1, 'stderr': err.output}
    os.unlink(infile.name)


def handle_rgw_create_user(request, service):
    user_id = request.get('rgw-uid')
    display_name = request.get('display-name')
    name = request.get('client-name')
    if not name or not display_name or not user_id:
        msg = "Missing client-name, display-name or rgw-uid"
        log(msg, level=ERROR)
        return {'exit-code': 1, 'stderr': msg}
    try:
        create_output = check_output(
            [
                'radosgw-admin',
                '--id', service,
                'user',
                'create',
                '--uid', user_id,
                '--display-name', display_name,
                '--name', name,
                '--system'
            ]
        )
        try:
            user_json = json.loads(create_output)
            return {'exit-code': 0, 'user': user_json}
        except ValueError as err:
            log(err, level=ERROR)
            return {'exit-code': 1, 'stderr': err}

    except CalledProcessError as err:
        log(err.output, level=ERROR)
        return {'exit-code': 1, 'stderr': err.output}


def handle_rgw_region_set(request, service):
    # radosgw-admin region set --infile us.json --name client.radosgw.us-east-1
    json_file = request.get('region-json')
    name = request.get('client-name')
    region_name = request.get('region-name')
    zone_name = request.get('zone-name')
    if not json_file or not name or not region_name or not zone_name:
        msg = "Missing json-file or client-name params"
        log(msg, level=ERROR)
        return {'exit-code': 1, 'stderr': msg}
    infile = NamedTemporaryFile(delete=False)
    with open(infile.name, 'w') as infile_handle:
        infile_handle.write(json_file)
    try:
        check_output(
            [
                'radosgw-admin',
                '--id', service,
                'region',
                'set',
                '--rgw-zone', zone_name,
                '--infile', infile.name,
                '--name', name,
            ]
        )
    except CalledProcessError as err:
        log(err.output, level=ERROR)
        return {'exit-code': 1, 'stderr': err.output}
    os.unlink(infile.name)


def process_requests_v1(reqs):
    """Process v1 requests.

    Takes a list of requests (dicts) and processes each one. If an error is
    found, processing stops and the client is notified in the response.

    Returns a response dict containing the exit code (non-zero if any
    operation failed along with an explanation).
    """
    ret = None
    log("Processing %s ceph broker requests" % (len(reqs)), level=INFO)
    for req in reqs:
        op = req.get('op')
        log("Processing op='%s'" % op, level=DEBUG)
        # Use admin client since we do not have other client key locations
        # setup to use them for these operations.
        svc = 'admin'
        if op == "create-pool":
            pool_type = req.get('pool-type')  # "replicated" | "erasure"

            # Default to replicated if pool_type isn't given
            if pool_type == 'erasure':
                ret = handle_erasure_pool(request=req, service=svc)
            else:
                ret = handle_replicated_pool(request=req, service=svc)

        elif op == "create-cache-tier":
            ret = handle_create_cache_tier(request=req, service=svc)
        elif op == "remove-cache-tier":
            ret = handle_remove_cache_tier(request=req, service=svc)
        elif op == "create-erasure-profile":
            ret = handle_create_erasure_profile(request=req, service=svc)
        elif op == "delete-pool":
            pool = req.get('name')
            ret = delete_pool(service=svc, name=pool)
        elif op == "rename-pool":
            old_name = req.get('name')
            new_name = req.get('new-name')
            ret = rename_pool(service=svc, old_name=old_name,
                              new_name=new_name)
        elif op == "snapshot-pool":
            pool = req.get('name')
            snapshot_name = req.get('snapshot-name')
            ret = snapshot_pool(service=svc, pool_name=pool,
                                snapshot_name=snapshot_name)
        elif op == "remove-pool-snapshot":
            pool = req.get('name')
            snapshot_name = req.get('snapshot-name')
            ret = remove_pool_snapshot(service=svc, pool_name=pool,
                                       snapshot_name=snapshot_name)
        elif op == "set-pool-value":
            ret = handle_set_pool_value(request=req, service=svc)
        elif op == "rgw-region-set":
            ret = handle_rgw_region_set(request=req, service=svc)
        elif op == "rgw-zone-set":
            ret = handle_rgw_zone_set(request=req, service=svc)
        elif op == "rgw-regionmap-update":
            ret = handle_rgw_regionmap_update(request=req, service=svc)
        elif op == "rgw-regionmap-default":
            ret = handle_rgw_regionmap_default(request=req, service=svc)
        elif op == "rgw-create-user":
            ret = handle_rgw_create_user(request=req, service=svc)
        else:
            msg = "Unknown operation '%s'" % op
            log(msg, level=ERROR)
            return {'exit-code': 1, 'stderr': msg}

    if type(ret) == dict and 'exit-code' in ret:
        return ret

    return {'exit-code': 0}
