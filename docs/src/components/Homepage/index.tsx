import React from "react";
import Link from "@docusaurus/Link";
import useBaseUrl from "@docusaurus/useBaseUrl";
import styles from "./styles.module.css";

type GuideCard = {
  title: string;
  description: string;
  to: string;
  label: string;
};

const GUIDES: GuideCard[] = [
  {
    title: "User Guide",
    description: "Call the OpenAI-compatible API and use the admin UI.",
    to: "/user/getting-started",
    label: "Get started",
  },
  {
    title: "Administrator Guide",
    description: "Install, configure, and operate a Logos deployment.",
    to: "/admin/installation",
    label: "Deploy Logos",
  },
  {
    title: "Developer Guide",
    description: "System design diagrams and local development setup.",
    to: "/developer/system-design",
    label: "System design",
  },
  {
    title: "Roles",
    description: "What each role can see and do in the web interface.",
    to: "/roles/app-developer",
    label: "Browse by role",
  },
];

const FACTS: { label: string; href?: string }[] = [
  { label: "OpenAI-compatible API" },
  { label: "Self-hosted vLLM workers" },
  { label: "Azure and OpenAI providers" },
  { label: "MIT License", href: "https://github.com/LogosOSS/Logos/blob/main/LICENSE" },
  { label: "Built at TU Munich", href: "https://aet.cit.tum.de/projects/ai/logos/" },
];

export default function Homepage(): React.JSX.Element {
  const architecture = useBaseUrl("/img/system-design/top-level.svg");
  const logo = useBaseUrl("/img/logos-logo.svg");
  const trailer = useBaseUrl("/video/logos-trailer.mp4");
  const trailerPoster = useBaseUrl("/video/logos-trailer-poster.jpg");

  return (
    <div className={styles.page}>
      <section className={styles.hero}>
        <div className={styles.heroSplit}>
          <div className={styles.heroText}>
            <img src={logo} alt="" className={styles.heroLogo} width={72} height={72} />
            <h1 className={styles.heroHeadline}>LLM Orchestration and Management</h1>
            <div className={styles.heroSubtitle}>
              One OpenAI-compatible gateway for self-hosted GPU workers and
              cloud providers, with request logs, billing, policies and team
              budgets.
            </div>
            <div className={styles.heroProof}>
              Chat Completions and Responses API
            </div>
            <div className={styles.heroCtas}>
              <Link className={styles.btnPrimary} to="/user/getting-started">
                Get Started
              </Link>
              <Link className={styles.btnGhost} to="/developer/system-design">
                System Design
              </Link>
            </div>
          </div>
          <div className={styles.heroVideo}>
            <video
              className={styles.trailer}
              src={trailer}
              poster={trailerPoster}
              controls
              playsInline
              preload="metadata"
              width={1920}
              height={1080}
              aria-label="Logos product trailer, 60 s, with music"
            />
            <div className={styles.heroVideoCaption}>Logos in 60 s</div>
          </div>
        </div>
      </section>

      <div className={styles.factBar}>
        {FACTS.map((fact) => (
          <span key={fact.label} className={styles.fact}>
            {fact.href ? <a href={fact.href}>{fact.label}</a> : fact.label}
          </span>
        ))}
      </div>

      <section className={styles.section}>
        <h2 className={styles.sectionHeading}>Guides</h2>
        <div className={styles.sectionSubtitle}>
          Four paths through the same platform — pick the one that matches
          what you are doing today.
        </div>
        <div className={styles.cardGrid}>
          {GUIDES.map((guide) => (
            <Link key={guide.to} className={styles.card} to={guide.to}>
              <h3 className={styles.cardTitle}>{guide.title}</h3>
              <p className={styles.cardBody}>{guide.description}</p>
              <span className={styles.cardCta}>{guide.label} →</span>
            </Link>
          ))}
        </div>
      </section>

      <section className={styles.sectionAlt}>
        <div className={styles.sectionAltInner}>
          <h2 className={styles.sectionHeading}>Architecture</h2>
          <div className={styles.sectionSubtitle}>
            Each client request goes through one gateway to a web service
            replica. The web service sends cloud requests to the cloud
            provider and local requests to the orchestrator. The GPU workers
            connect to the orchestrator over WebSocket.
          </div>
          <Link className={styles.diagramLink} to="/developer/system-design">
            <img
              src={architecture}
              alt="UML component diagram of Logos: each client request goes through the gateway to the logos-webservice replicas"
              className={styles.diagram}
            />
          </Link>
          <p className={styles.diagramCaption}>
            Top-level design, deployment, data model, and request flow live in{" "}
            <Link to="/developer/system-design">System Design</Link>. Terminology
            is in the{" "}
            <Link to="/developer/architecture">architecture reference</Link>.
          </p>
        </div>
      </section>
    </div>
  );
}
