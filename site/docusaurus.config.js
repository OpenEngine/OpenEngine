// @ts-check
import {themes as prismThemes} from 'prism-react-renderer';

const REPO = 'https://github.com/OpenEngine/OpenEngine';

/** @type {import('@docusaurus/types').Config} */
const config = {
  title: 'OpenEngine',
  tagline: 'Your Software Factory',
  favicon: 'favicon.svg',
  url: 'https://openengine.sh',
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
          editUrl: `${REPO}/edit/main/site/`,
        },
        blog: false,
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
          {href: 'mailto:hello@openengine.sh', position: 'right', label: 'Contact Us', className: 'navbar-cta'},
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
