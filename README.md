# JWST Engineering Telemetry Archive (JETA)

A python package for getting data into and out of the JWST Engineering Telemetery Archive.

The package, `jeta` is a jSka functional-equivalent of [`Ska.eng_archive`](https://github.com/sot/eng_archive).
[Ska](https://cxc.cfa.harvard.edu/mta/ASPECT/tool_doc/pydocs/) is the "engineering telemetry archive is a suite of tools and data products" for
that supports the [Chandra](https://chandra.harvard.edu/about/spacecraft.html) X-Ray Observatiory. Where as `jeta` fulfills a similar role for JWST.

> NOTE: The project does not contain any telemetry data, just the tools.

## Getting Started

TBD

### Prerequisites

All package dependencies and support scripts are build into the container.

### Environment Variables

TBD

### Installing

TBD

## Running the tests

```bash
coverage run -m  pytest .
coverage html
```

## Coding Style

https://github.com/spacetelescope/style-guides

## Built With

* [Chandra Tools](https://cxc.harvard.edu/mta/ASPECT/tool_doc/pydocs/index.html) - Built around Chandra Tools

## Versioning

Project adhears to [Semantic Versioning](https://semver.org/spec/v2.0.0.html). for versioning.


## Authors

* **David Kauffman** - *Initial work* - [David Kauffman](https://github.com/ddkauffman)

## Acknowledgments

* Amanda Arvai
* Tom Aldcroft
* Jean Connelly
* Alex Hunter