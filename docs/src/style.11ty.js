// Concatenates the CUBE layers into a single stylesheet, in cascade order.
//
// The layers live in _includes/css/ and are numbered, because in CUBE the order
// is the point: Tokens, then Global axioms, then Compositions, Utilities,
// Blocks, and finally Exceptions. Each layer is allowed to override the one
// before it, so shipping them out of order would quietly break the methodology.

import { readdir, readFile } from "node:fs/promises";
import path from "node:path";

const CSS_DIR = path.join(import.meta.dirname, "_includes", "css");

export const data = {
  permalink: "/assets/style.css",
  eleventyExcludeFromCollections: true,
};

export async function render() {
  const files = (await readdir(CSS_DIR)).filter((name) => name.endsWith(".css")).sort();

  if (files.length === 0) {
    throw new Error(`no CSS layers found in ${CSS_DIR}`);
  }

  const layers = await Promise.all(
    files.map(async (name) => {
      const css = await readFile(path.join(CSS_DIR, name), "utf8");
      return `/* ${name} */\n${css.trim()}`;
    }),
  );

  return layers.join("\n\n");
}
