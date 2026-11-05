import { createRequire } from 'node:module';
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';
/** Use only the installed host; the fallback is for direct SDK tests. */
export async function hostModule(name) {
  try { return await import(name); }
  catch (error) {
    if (error.code !== 'ERR_MODULE_NOT_FOUND') throw error;
    // The launcher maps DSH packages, while a linked plugin's own directory
    // cannot see their transitive dependencies. Resolve the host SDK's Zod,
    // not a second installation in the plugin or a hardcoded machine path.
    if (name === 'zod' && !process.env.DSH_TEST_RUNTIME) {
      const requireSdk = createRequire(import.meta.resolve('@deepseek-ai/dsh-tools'));
      return import(pathToFileURL(requireSdk.resolve('zod')));
    }
    if (!process.env.DSH_TEST_RUNTIME) throw error;
    const requireHost = createRequire(join(process.env.DSH_TEST_RUNTIME, 'package.json'));
    return import(pathToFileURL(requireHost.resolve(name)));
  }
}
