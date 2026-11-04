import React, { useRef, useState } from "react";
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

export default function Homepage(): React.JSX.Element {
  const architecture = useBaseUrl("/img/system-design/top-level.svg");
  const trailer = useBaseUrl("/video/logos-trailer.mp4");
  const trailerPoster = useBaseUrl("/video/logos-trailer-poster.jpg");
  // The native controls would cover the poster text on small screens, so they
  // appear only after the reader starts the video.
  const video = useRef<HTMLVideoElement>(null);
  const [started, setStarted] = useState(false);
  const play = () => {
    setStarted(true);
    void video.current?.play();
  };

  return (
    <div className={styles.page}>
      <section className={styles.hero}>
        <div className={styles.heroInner}>
          <h1 className={styles.heroHeadline}>LLM engineering made easy</h1>
          <div className={styles.heroSubtitle}>
            OpenAI-compatible inference with logging, billing, policies, and
            self-hosted workers — documented for users, operators, and
            contributors.
          </div>
          <div className={styles.heroCtas}>
            <Link className={styles.btnPrimary} to="/user/getting-started">
              Start with the User Guide
            </Link>
            <Link className={styles.btnGhost} to="/developer/system-design">
              View system design
            </Link>
          </div>
        </div>
        <div className={styles.heroVideo}>
          <div className={styles.videoFrame}>
          <video
            ref={video}
            className={styles.trailer}
            src={trailer}
            poster={trailerPoster}
            controls={started}
            playsInline
            preload="metadata"
            width={1920}
            height={1080}
            aria-label="Logos product trailer, 60 s, with music"
          />
          {!started && (
            <button type="button" className={styles.playButton} onClick={play} aria-label="Play the Logos trailer">
              <svg viewBox="0 0 24 24" width="30" height="30" aria-hidden="true">
                <path d="M8 5.5v13l11-6.5z" fill="currentColor" />
              </svg>
            </button>
          )}
          </div>
          <div className={styles.heroVideoCaption}>Logos in 60 s</div>
        </div>
      </section>

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
