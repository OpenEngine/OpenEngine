// @ts-check

/** @type {import('@docusaurus/plugin-content-docs').SidebarsConfig} */
const sidebars = {
  docs: [
    {type: 'category', label: 'Getting started', collapsible: false, items: ['index']},
    {type: 'category', label: 'Concepts', collapsible: false, items: ['graphs', 'loops']},
    {type: 'category', label: 'Reference', collapsible: false, items: ['cli-reference', 'cli-specs']},
    {type: 'category', label: 'Integrations', collapsible: false, link: {type: 'doc', id: 'integrations'}, items: ['integrations/slack', 'integrations/github', 'integrations/mcp']},
  ],
};

export default sidebars;
