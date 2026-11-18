const assert = require('node:assert/strict');
const { test } = require('node:test');
const { cleanup, packages } = require('../cleanup-ghcr.cjs');

function version(id, ...tags) {
  return { id, metadata: { container: { tags } } };
}

function fixture({ versions = [], current, state = 'closed', status, stage } = {}) {
  const removed = [];
  const listed = [];
  const reads = [];
  const error = Object.assign(new Error('API failure'), { status });
  const github = {
    rest: {
      pulls: { get: async () => ({ data: { state: typeof state === 'function' ? state() : state } }) },
      packages: {
        getAllPackageVersionsForPackageOwnedByOrg: () => {},
        getPackageVersionForOrganization: async ({ package_version_id }) => {
          reads.push(package_version_id);
          if (stage === 'get') throw error;
          return { data: current || versions.find(item => item.id === package_version_id) };
        },
        deletePackageVersionForOrg: async params => {
          if (stage === 'delete') throw error;
          removed.push(params);
        },
      },
    },
    paginate: async (method, params) => {
      assert.equal(method, github.rest.packages.getAllPackageVersionsForPackageOwnedByOrg);
      assert.equal(params.per_page, 100);
      assert.equal(params.org, 'LogosOSS');
      assert.equal(params.package_type, 'container');
      listed.push(params.package_name);
      if (stage === 'list') throw error;
      return params.package_name === packages[0] ? versions : [];
    },
  };
  const context = { repo: { owner: 'LogosOSS', repo: 'Logos' }, payload: { pull_request: { number: 42 } } };
  return { removed, listed, reads, run: () => cleanup({ github, context, core: { info() {} } }) };
}

test('deletes only exclusive PR versions, retaining shared tags and untagged manifests', async () => {
  const f = fixture({ versions: [
    version(1, 'pr-42'), version(2, 'pr-42', 'latest'), version(3, 'pr-42', '1.2.3'),
    version(4), version(5, 'pr-420'), version(6, 'latest'), { id: 7 },
  ] });
  await f.run();
  assert.deepEqual(f.removed.map(item => item.package_version_id), [1]);
  assert.deepEqual(f.reads, [1]);
  assert.deepEqual(f.listed, packages);
});

test('processes the complete paginated snapshot, including versions after the first hundred', async () => {
  const versions = Array.from({ length: 101 }, (_, id) => version(id, 'latest'));
  versions.push(version(101, 'pr-42'));
  const f = fixture({ versions });
  await f.run();
  assert.deepEqual(f.removed.map(item => item.package_version_id), [101]);
});

test('keeps a version that gained a live tag after the listing', async () => {
  const f = fixture({ versions: [version(1, 'pr-42')], current: version(1, 'pr-42', 'latest') });
  await f.run();
  assert.deepEqual(f.removed, []);
});

test('does not enumerate packages for a reopened PR', async () => {
  const f = fixture({ state: 'open' });
  await f.run();
  assert.deepEqual(f.listed, []);
});

test('stops deletion if a PR reopens during enumeration', async () => {
  let calls = 0;
  const f = fixture({ versions: [version(1, 'pr-42')], state: () => ++calls === 1 ? 'closed' : 'open' });
  await f.run();
  assert.deepEqual(f.removed, []);
});

for (const stage of ['list', 'get', 'delete']) {
  test(`tolerates an already missing package/version during ${stage}`, async () => {
    const f = fixture({ versions: [version(1, 'pr-42')], stage, status: 404 });
    await f.run();
    assert.deepEqual(f.removed, []);
  });
  for (const status of [401, 403, 500]) {
    test(`reports HTTP ${status} during ${stage}`, async () => {
      const f = fixture({ versions: [version(1, 'pr-42')], stage, status });
      await assert.rejects(f.run, { status });
    });
  }
}
