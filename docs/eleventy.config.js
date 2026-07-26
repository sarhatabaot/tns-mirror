// Eleventy configuration for the tns-mirror documentation site.
//
// Liquid throughout: layouts, partials, and the markdown pages themselves. Kept
// deliberately small — the docs are content, not an application. A page that
// needs to show literal Liquid delimiters wraps them in a raw block.

import syntaxHighlight from "@11ty/eleventy-plugin-syntaxhighlight";
import markdownItAnchor from "markdown-it-anchor";

export default function (eleventyConfig) {
  // Prism runs at build time, so highlighted code is baked into the HTML and no
  // highlighting JavaScript ships to the reader. Colours are in 06-syntax.css.
  eleventyConfig.addPlugin(syntaxHighlight);
  // Unknown filters fail the build rather than rendering as nothing, so a typo
  // in a template is a broken build and not a silently empty page.
  eleventyConfig.setLiquidOptions({ strictFilters: true });

  // Headings get ids so the on-this-page navigation and deep links work.
  // Slugs are stripped of punctuation: the default keeps it, which turns
  // "Why not just write the SQL?" into a fragment ending in %3F.
  const slugify = (text) =>
    text
      .toLowerCase()
      .replace(/[^\w\s-]/g, "")
      .trim()
      .replace(/\s+/g, "-");

  eleventyConfig.amendLibrary("md", (mdLib) =>
    mdLib.use(markdownItAnchor, {
      level: [2, 3],
      slugify,
      permalink: markdownItAnchor.permalink.ariaHidden({
        class: "heading-anchor",
        symbol: "#",
        placement: "after",
      }),
    }),
  );

  // Pages are grouped into sections — Server, Client, Project — because a reader
  // is usually one or the other: an operator running a mirror, or a consumer
  // querying one somebody else runs.
  eleventyConfig.addCollection("docs", (collectionApi) =>
    collectionApi
      .getFilteredByTag("docs")
      .sort((a, b) => (a.data.order ?? 99) - (b.data.order ?? 99)),
  );

  eleventyConfig.addFilter("whereSection", (pages, section) =>
    (pages ?? []).filter((page) => page.data.section === section),
  );

  // On-this-page navigation, built from the rendered HTML rather than the
  // markdown source, so it reflects what a reader actually sees.
  eleventyConfig.addFilter("headings", (html) => {
    const found = [];
    const pattern = /<h([23])[^>]*\sid="([^"]+)"[^>]*>(.*?)<\/h\1>/gis;
    let match;
    while ((match = pattern.exec(html ?? "")) !== null) {
      found.push({
        level: Number(match[1]),
        id: match[2],
        text: match[3]
          // Drop the permalink anchor *with* its "#" text, then any remaining
          // inline markup. Stripping tags alone would leave the # behind.
          .replace(/<a\b[^>]*class="heading-anchor"[^>]*>.*?<\/a>/gis, "")
          .replace(/<[^>]*>/g, "")
          .trim(),
      });
    }
    return found;
  });

  return {
    dir: {
      input: "src",
      output: "_site",
      includes: "_includes",
      data: "_data",
    },
    // GitHub Pages serves a project site under /<repo>/. The deploy workflow
    // sets ELEVENTY_PATH_PREFIX=/tns-mirror/; locally the default is fine.
    pathPrefix: process.env.ELEVENTY_PATH_PREFIX || "/",
    markdownTemplateEngine: "liquid",
    htmlTemplateEngine: "liquid",
    // 11ty.js is here only for the stylesheet bundle; all content is Liquid.
    templateFormats: ["md", "liquid", "11ty.js"],
  };
}
