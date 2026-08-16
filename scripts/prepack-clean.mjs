// prepack hook: strip __pycache__/*.pyc from the tree before npm pack/publish.
// npm 11 does not apply .npmignore rules to directories listed in the "files"
// field, so a stale __pycache__ would ship inside the tarball otherwise.
import { readdirSync, rmSync } from 'node:fs';
import { join } from 'node:path';

function clean(dir, depth = 0) {
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    if (!entry.isDirectory()) continue;
    if (entry.name === 'node_modules') continue;
    const p = join(dir, entry.name);
    if (entry.name === '__pycache__') {
      rmSync(p, { recursive: true, force: true });
    } else {
      clean(p, depth + 1);
    }
  }
}

clean(process.cwd());
