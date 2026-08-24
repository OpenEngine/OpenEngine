const PLACEHOLDER_MILESTONES = [
  "Milestone 1",
  "Milestone 2",
  "Milestone 3",
  "Milestone 4",
] as const;

/** The planning canvas starts as a deliberately simple timeline. The planner
 *  will replace these placeholders with project milestones once plan data is
 *  exposed to the web app. */
export function MilestoneVisualizer() {
  return (
    <section className="milestone-visualizer" aria-labelledby="milestone-title">
      <header className="milestone-head">
        <div>
          <p className="eyebrow">Project plan</p>
          <h2 id="milestone-title">Milestone timeline</h2>
        </div>
        <span className="micro">Evenly spaced</span>
      </header>
      <ol className="milestone-track">
        {PLACEHOLDER_MILESTONES.map((milestone, index) => (
          <li className="milestone" key={milestone}>
            <span className="milestone-marker" aria-hidden="true" />
            <span className="milestone-number">{String(index + 1).padStart(2, "0")}</span>
            <span className="milestone-name">{milestone}</span>
          </li>
        ))}
      </ol>
    </section>
  );
}
