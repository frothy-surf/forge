// Convert a skopeo OCI layout into the registry worker's R2 key layout
// (registry/src/index.ts): <repo>/blobs/sha256:<hex> and
// <repo>/manifests/{<tag>, sha256:<digest>}. Output is a staging directory
// for `aws s3 sync` — colons are legal in both filenames and R2 keys.
//
//   skopeo copy --all docker://ghcr.io/…/frothy-forge:latest oci:/tmp/oci:latest
//   node scripts/oci-to-r2.mjs /tmp/oci frothy-forge latest /tmp/registry-stage
import { copyFileSync, mkdirSync, readFileSync, readdirSync } from "node:fs";
import { join } from "node:path";

const [layout, repo, tag, out] = process.argv.slice(2);
if (!out) {
  console.error("usage: oci-to-r2.mjs <oci-layout-dir> <repo> <tag> <staging-dir>");
  process.exit(1);
}

const blobsIn = join(layout, "blobs", "sha256");
const blobsOut = join(out, repo, "blobs");
const manOut = join(out, repo, "manifests");
mkdirSync(blobsOut, { recursive: true });
mkdirSync(manOut, { recursive: true });

// every blob (layers, configs, manifests) pull-able by digest
let blobs = 0;
for (const hex of readdirSync(blobsIn)) {
  copyFileSync(join(blobsIn, hex), join(blobsOut, `sha256:${hex}`));
  blobs++;
}

// the tag's manifest: index.json holds descriptors keyed by ref.name
const index = JSON.parse(readFileSync(join(layout, "index.json"), "utf8"));
const desc =
  index.manifests.find(
    (m) => m.annotations?.["org.opencontainers.image.ref.name"] === tag,
  ) ?? index.manifests[0];
if (!desc) {
  console.error("no manifest descriptor in index.json");
  process.exit(1);
}
const tagHex = desc.digest.replace("sha256:", "");
copyFileSync(join(blobsIn, tagHex), join(manOut, tag));
copyFileSync(join(blobsIn, tagHex), join(manOut, desc.digest));

// multi-arch: child image manifests must also resolve under manifests/
const top = JSON.parse(readFileSync(join(blobsIn, tagHex), "utf8"));
let children = 0;
if (Array.isArray(top.manifests)) {
  for (const child of top.manifests) {
    const hex = child.digest.replace("sha256:", "");
    copyFileSync(join(blobsIn, hex), join(manOut, child.digest));
    children++;
  }
}

console.log(
  `${repo}:${tag} staged — ${blobs} blob(s), top manifest ${desc.digest.slice(0, 19)}…` +
    (children ? `, ${children} arch manifest(s)` : ""),
);
