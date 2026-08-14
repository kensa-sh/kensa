import {
  generateRepositoryBuildManifest,
  repositoryRoot,
} from "./build-manifest.mjs";

await generateRepositoryBuildManifest(process.argv.slice(2), repositoryRoot);
