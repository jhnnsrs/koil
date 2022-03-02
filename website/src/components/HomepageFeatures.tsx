import useBaseUrl from "@docusaurus/useBaseUrl";
import React from "react";
import clsx from "clsx";
import styles from "./HomepageFeatures.module.css";

type FeatureItem = {
  title: string;
  image: string;
  description: JSX.Element;
};

const FeatureList: FeatureItem[] = [
  {
    title: "Easy to Use",
    image: "/img/undraw_docusaurus_mountain.svg",
    description: (
      <>
        Koil was designed from the ground up to be easily installed and used
        with your favorite technologies.
      </>
    ),
  },
  {
    title: "Contextify",
    image: "/img/undraw_docusaurus_tree.svg",
    description: (
      <>
        Use async context managers in a synchronous way. Koil takes care of
        spinning up eventloops and tearing them down.
      </>
    ),
  },
  {
    title: "Powered by Async",
    image: "/img/undraw_docusaurus_react.svg",
    description: (
      <>
        Koil is just a thin wrapper around the asyncio library, with additional
        support for qtpy if needed.
      </>
    ),
  },
];

function Feature({ title, image, description }: FeatureItem) {
  return (
    <div className={clsx("col col--4")}>
      <div className="text--center padding-horiz--md padding-top--md">
        <h3>{title}</h3>
        <p>{description}</p>
      </div>
    </div>
  );
}

export default function HomepageFeatures(): JSX.Element {
  return (
    <section className={styles.features}>
      <div className="container">
        <div className="row">
          {FeatureList.map((props, idx) => (
            <Feature key={idx} {...props} />
          ))}
        </div>
      </div>
    </section>
  );
}
