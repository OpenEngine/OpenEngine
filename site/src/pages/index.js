import Link from '@docusaurus/Link';
import useDocusaurusContext from '@docusaurus/useDocusaurusContext';
import Layout from '@theme/Layout';
import styles from './index.module.css';

// The shipped workflow, drawn as one change travelling through it. Each node
// lights up as the pulse reaches it; `at` is that moment in the loop.
const REVIEWERS = ['security', 'bugs', 'performance', 'conciseness', 'dryness'];
const REVIEWER_Y = [12, 72, 132, 192, 252];

function Node({x, y, w, label, at = 0, person = false}) {
  return (
    <g className={person ? styles.person : styles.node} style={{'--at': `${at}s`}}>
      <rect x={x} y={y} width={w} height={36} />
      <text x={x + w / 2} y={y + 18} textAnchor="middle" dominantBaseline="central">
        {label}
      </text>
    </g>
  );
}

function Edge({d, at}) {
  return (
    <>
      <path className={styles.edge} d={d} />
      <path className={styles.pulse} style={{'--at': `${at}s`}} d={d} />
    </>
  );
}

function WorkflowGraph() {
  return (
    <svg
      viewBox="0 0 980 330"
      role="img"
      aria-label="Implement, CI, five parallel reviewers, rerank, impact analysis, human review">
      <path className={`${styles.edge} ${styles.loop}`} d="M565 168 V320 H70 V168" />
      <text className={styles.loopLabel} x="318" y="312" textAnchor="middle">
        findings go back for one fix-and-review round
      </text>
      <Edge d="M130 150 H170" at={0.25} />
      {REVIEWER_Y.map((y) => (
        <g key={y}>
          <Edge d={`M260 150 C285 150 285 ${y + 18} 310 ${y + 18}`} at={0.75} />
          <Edge d={`M460 ${y + 18} C485 ${y + 18} 485 150 510 150`} at={1.5} />
        </g>
      ))}
      <Edge d="M620 150 H660" at={2.2} />
      <Edge d="M780 150 H820" at={2.7} />
      <Node x={10} y={132} w={120} label="implement" />
      <Node x={170} y={132} w={90} label="CI" at={0.5} />
      {REVIEWERS.map((name, i) => (
        <Node key={name} x={310} y={REVIEWER_Y[i]} w={150} label={name} at={1.1} />
      ))}
      <Node x={510} y={132} w={110} label="rerank" at={1.9} />
      <Node x={660} y={132} w={120} label="impact" at={2.4} />
      <Node x={820} y={132} w={150} label="human review" person />
    </svg>
  );
}

const LOGOS = [
  ['claude', 'Claude Code'],
  ['openai', 'Codex'],
  ['langchain', 'LangGraph'],
  ['github', 'GitHub'],
  ['slack', 'Slack'],
];

const PILLARS = [
  ['workflows', 'Workflows', 'Your lifecycle as a graph. Add a reviewer once and it checks every change.'],
  ['workorders', 'WorkOrders', 'One task, run through the graph. One model family builds, another reviews.'],
  ['integrations', 'Integrations', 'Start, steer and approve work from Slack threads and pull request comments.'],
];

export default function Home() {
  const {siteConfig} = useDocusaurusContext();
  return (
    <Layout
      title="Your Software Factory"
      description="OpenEngine runs your software development lifecycle as a LangGraph workflow, on the Claude Code and Codex subscriptions you already have.">
      <main className={styles.landing}>
        <div className={styles.wrap}>
          <div className={styles.hero}>
            <p className={`${styles.eyebrow} ${styles.rise}`}>Open source · Apache-2.0</p>
            <h1 className={styles.rise}>
              Your Software <em>Factory</em>
            </h1>
            <p className={`${styles.lede} ${styles.rise}`}>
              Write your development lifecycle once, in LangGraph. OpenEngine runs every change through it on Claude
              Code and Codex, and hands you a reviewed pull request.
            </p>
            <div className={`${styles.actions} ${styles.rise}`}>
              <Link className={`${styles.btn} ${styles.btnPrimary}`} to="/docs/">
                Read the docs
              </Link>
              <Link className={styles.btn} href={siteConfig.customFields.repo}>
                GitHub
              </Link>
            </div>
          </div>

          <figure className={styles.graph}>
            <div className={styles.graphHead}>
              <span className={styles.eyebrow}>implementation-review-rerank</span>
              <span className={styles.eyebrow}>the workflow that builds OpenEngine</span>
            </div>
            <WorkflowGraph />
          </figure>
        </div>

        <section className={styles.logos}>
          <div className={styles.wrap}>
            <span className={styles.eyebrow}>Runs on what you already use</span>
            {LOGOS.map(([file, name]) => (
              <span key={file} className={styles.logo}>
                <img src={`/img/logos/${file}.svg`} alt="" />
                {name}
              </span>
            ))}
          </div>
        </section>

        <div className={styles.wrap}>
          <div className={styles.pillars}>
            {PILLARS.map(([slug, title, body], i) => (
              <Link key={slug} className={styles.pillar} to={`/docs/${slug}/`}>
                <span className={styles.eyebrow}>0{i + 1}</span>
                <h2>{title}</h2>
                <p>{body}</p>
                <span className={styles.more}>{title} →</span>
              </Link>
            ))}
          </div>
        </div>
      </main>
    </Layout>
  );
}
