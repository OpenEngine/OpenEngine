// @ts-check
import {themes as prismThemes} from 'prism-react-renderer';

const REPO = 'https://github.com/OpenEngine/OpenEngine';

/** @type {import('@docusaurus/types').Config} */
const config = {
  title: 'OpenEngine',
  tagline: 'The open-source software factory: define your lifecycle as a graph, run every change through it on Claude Code and Codex.',
  favicon: 'favicon.svg',
  url: 'https://openengine.cc',
  baseUrl: '/',
  organizationName: 'OpenEngine',
  projectName: 'OpenEngine',
  trailingSlash: true,
  onBrokenLinks: 'throw',
  markdown: {hooks: {onBrokenMarkdownLinks: 'throw'}},
  customFields: {repo: REPO},

  headTags: [
    {tagName: 'link', attributes: {rel: 'preconnect', href: 'https://fonts.googleapis.com'}},
    {tagName: 'link', attributes: {rel: 'preconnect', href: 'https://fonts.gstatic.com', crossorigin: 'anonymous'}},
    {
      tagName: 'link',
      attributes: {
        rel: 'stylesheet',
        href: 'https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=JetBrains+Mono:wght@400;500;600&display=swap',
      },
    },
  ],

  presets: [
    [
      'classic',
      /** @type {import('@docusaurus/preset-classic').Options} */
      ({
        docs: {
          routeBasePath: 'docs',
          sidebarPath: './sidebars.js',
          // The Guides category links to its landing page already; don't list it again among the guides.
          async sidebarItemsGenerator({defaultSidebarItemsGenerator, ...args}) {
            const items = await defaultSidebarItemsGenerator(args);
            return items.filter((item) => !(item.type === 'doc' && item.id === 'guides/index'));
          },
          // Generated guides have no source here to edit; they come from e2e/tests.
          editUrl: ({versionDocsDirPath, docPath}) =>
            docPath.startsWith('guides/test2doc-') ? undefined : `${REPO}/edit/main/site/${versionDocsDirPath}/${docPath}`,
        },
        blog: {
          routeBasePath: 'blog',
          showReadingTime: true,
          onInlineAuthors: 'throw',
          onUntruncatedBlogPosts: 'throw',
          editUrl: `${REPO}/edit/main/site/`,
        },
        theme: {customCss: './src/css/custom.css'},
      }),
    ],
  ],

  themeConfig:
    /** @type {import('@docusaurus/preset-classic').ThemeConfig} */
    ({
      colorMode: {defaultMode: 'dark', respectPrefersColorScheme: false},
      navbar: {
        title: 'OpenEngine',
        logo: {alt: '', src: 'favicon.svg'},
        items: [
          {type: 'docSidebar', sidebarId: 'docs', position: 'right', label: 'Docs'},
          {to: '/blog', position: 'right', label: 'Blog'},
          {href: REPO, position: 'right', label: 'GitHub', className: 'navbar-github', 'aria-label': 'Star OpenEngine on GitHub'},
          {href: 'mailto:hello@openengine.cc', position: 'right', label: 'Contact Us', className: 'navbar-cta'},
        ],
      },
      footer: {
        copyright: `Apache-2.0 · <a href="${REPO}">github.com/OpenEngine/OpenEngine</a>`,
      },
      prism: {
        theme: prismThemes.github,
        darkTheme: prismThemes.vsDark,
        additionalLanguages: ['toml', 'bash'],
      },
    }),
};

export default config;
