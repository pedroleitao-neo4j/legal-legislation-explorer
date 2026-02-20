# A UK Legislation Graph Explorer

> ***Work in progress***, this is not yet reliable enough for 100% fidelity extraction of the entire UK legislation corpus, but it is a starting point for building a graph database of UK legislation, and it is designed to be easily extensible and adaptable to handle the complexities of the UK legislation XML format.

This repository contains a [crawler](crawler.ipynb) and [data loader](data_loader.ipynb) for [UK Legislation](https://www.legislation.gov.uk/). It is intended to be used as a starting point for building a graph database of UK legislation, **without** the need for a complex ETL pipeline, PDF processing, manual data cleaning, or other time-consuming and error-prone processes. The crawler directly extracts structured data from the XML files provided by legislation.gov.uk, and the data loader transforms this structured data into a graph format suitable for loading into Neo4j.

The XML format for UK legislation is known to be fiendishly complex, and the crawler and data loader are designed to handle this complexity. The crawler extracts the (almost) full hierarchy of legislation, including Parts, Chapters, Sections, Paragraphs, Schedules, and Subparagraphs. It also extracts citations and cross-references between different pieces of legislation, as well as commentaries and other related information.

> The [loader](loader.ipynb) currently uses [pyspark](https://spark.apache.org/docs/latest/api/python/index.html) to transform the raw JSON data into a format suitable for loading into Neo4j. This is because the transformation process involves some data manipulation, and pyspark provides a powerful and flexible way to handle this. However, it is possible to refactor the loader to use plain Python if desired.

### Example Queries

All legislation which directly or indirectly connects to it.
```cypher
// Anchor the query on the target legislation
MATCH (target:Legislation)
WHERE target.uri CONTAINS "uksi/2020/1495"
// Traverse backwards from the target, up through the citation nodes and the structural hierarchy to the source Act
MATCH p = (source:Legislation)-[:HAS_PART|HAS_CHAPTER|HAS_SECTION|HAS_PARAGRAPH|HAS_SCHEDULE|HAS_SUBPARAGRAPH|HAS_COMMENTARY|HAS_CITATION|HAS_SUBREF*1..5]->(citation_link)-[:CITES_ACT|REFERENCES]->(target)
// Exclude self-citations (where the source Act is the same as the target Act)
WHERE source.uri <> target.uri
RETURN p
```

A legislation down to its sections and paragraphs.
```cypher
match p=(l:Legislation)-[:HAS_PART]->(:Part)-[:HAS_CHAPTER]->(:Chapter)-[:HAS_SECTION]->(:Section)-[:HAS_PARAGRAPH]->(:Paragraph)
WHERE l.uri CONTAINS "ukpga/2010/4"
RETURN p
```

All commentaries which cite a legislation.
```cypher
MATCH p=(:Commentary)-[:HAS_CITATION]->(:Citation)-[:CITES_ACT]->(l:Legislation)
WHERE l.uri CONTAINS "uksi/2020/1495"
RETURN p
```

All paragraphs which have commentary which cite a Legislation
```cypher
MATCH p=(:Paragraph)-[:HAS_COMMENTARY]->(:Commentary)-[:HAS_CITATION]->(:Citation)-[:CITES_ACT]->(l:Legislation)
WHERE l.uri CONTAINS "uksi/2020/1495"
RETURN p
```

All Schedules and their paragraphs, together with any commentary which cite a Legislation
```cypher
MATCH p=(l:Legislation)-[:HAS_SCHEDULE]->(sc:Schedule)-[:HAS_PARAGRAPH]->(scp:ScheduleParagraph)-[:HAS_SUBPARAGRAPH]->(scsp:ScheduleSubparagraph)
WHERE l.uri CONTAINS "ukpga/2010/4"
OPTIONAL MATCH (scp)-[:HAS_COMMENTARY]-(:Commentary)-[:HAS_CITATION]-(:Citation)-[:HAS_SUBREF]->(:CitationSubRef)
RETURN p
```

## TODO

### Unique IDs

~~ID's such as commentary, and ref IDs are not unique across the entire dataset. They are only unique within the context of a single legislation. This means that we need to generate unique IDs for these entities when we load them into the graph database.~~

### Citations, Sub-Refs, and Commentary

It is currently a bit messy and redundant, needs to be refactored.