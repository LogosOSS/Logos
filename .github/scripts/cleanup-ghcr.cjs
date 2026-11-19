const packages = Object.freeze([
  'logos-orchestrator',
  'logos-webservice',
  'logos-ui',
  'logos-db',
  'logos-workernode-vllm',
  'logos-workernode-mlx',
  'logos-node-manager',
  'logos-agent',
  'logos-agent-gateway',
  'logos-rate-gateway',
  'logos-agent-workspace',
]);

function exclusiveTag(version, tag) {
  const tags = version.metadata?.container?.tags;
  return tags?.length === 1 && tags[0] === tag;
}

async function missingIsEmpty(operation) {
  try {
    return await operation();
  } catch (error) {
    if (error.status === 404) return undefined;
    throw error;
  }
}

async function cleanup({ github, context, core }) {
  const pull_number = context.payload.pull_request.number;
  const pull = await github.rest.pulls.get({ ...context.repo, pull_number });
  if (pull.data.state !== 'closed') {
    core.info(`PR ${pull_number} is open; keeping its images.`);
    return;
  }

  const tag = `pr-${pull_number}`;
  for (const package_name of packages) {
    const params = { org: context.repo.owner, package_type: 'container', package_name };
    // Snapshot all pages before deleting, so pagination cannot skip versions.
    const versions = await missingIsEmpty(() => github.paginate(
      github.rest.packages.getAllPackageVersionsForPackageOwnedByOrg,
      { ...params, per_page: 100 },
    ));
    for (const version of versions || []) {
      // GHCR deletes entire versions, not individual tags. Keep shared tags
      // and untagged manifests, which may be children of a live image index.
      if (!exclusiveTag(version, tag)) continue;
      const target = { ...params, package_version_id: version.id };
      const current = await missingIsEmpty(() => github.rest.packages.getPackageVersionForOrganization(target));
      if (!current || !exclusiveTag(current.data, tag)) continue;
      // A PR can be reopened while packages are being enumerated.
      const latestPull = await github.rest.pulls.get({ ...context.repo, pull_number });
      if (latestPull.data.state !== 'closed') return;
      await missingIsEmpty(() => github.rest.packages.deletePackageVersionForOrg(target));
      core.info(`Removed ${package_name}:${tag} (version ${version.id}).`);
    }
  }
}

module.exports = { cleanup, packages };
