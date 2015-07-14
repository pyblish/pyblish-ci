import os
import sys
import json
import time
import logging
# import shutil
import tempfile
import traceback
import threading
import contextlib
import subprocess

# Dependencies
import yaml
import flask
import requests
import flask.ext.restful

app = flask.Flask(__name__)

formatter = logging.Formatter("%(levelname)s - %(message)s")
handler = logging.StreamHandler()
handler.setFormatter(formatter)

self = sys.modules[__name__]
self.app = app
self.cache = {}
self.pages = {}
self.root = "/ci"

self.log = logging.getLogger()
self.log.propagate = True
self.log.handlers[:] = []
self.log.addHandler(handler)
self.log.setLevel(logging.DEBUG)


@contextlib.contextmanager
def tempdir():
    tempdir = tempfile.mkdtemp()
    try:
        yield tempdir
    finally:
        pass
        # time.sleep(1)
        # shutil.rmtree(tempdir)


def run_script(job, pull_request):
    self.log.info("Running tests..")

    clone_url = pull_request["base"]["repo"]["clone_url"]
    number = pull_request["number"]

    with tempdir() as d:
        subprocess.check_output(
            ["git", "clone", clone_url, d])
        subprocess.check_output(
            ["git", "fetch", "origin", "pull/%s/head:pr" % number], cwd=d)
        subprocess.check_output(
            ["git", "checkout", "pr"], cwd=d)

        self.log.info("Running job: %s" % job)

        # Parse configuration
        config = os.path.join(d, ".pyblish")
        if not os.path.exists(config):
            self.log.info("No .pyblish file found")
            return

        with open(config) as f:
            config = yaml.load(f)

        # Get environment
        environment = config.get("environment", "mottosso/maya2015-centos")
        script = "\n".join(config.get("script", []))

        # Convert .pyblish:script to script.sh
        script_sh = os.path.join(d, "script.sh")
        with open(script_sh, "w") as f:
            f.write(script)

        self.log.info("Running script:\n%s" % script)
        cmd = ["docker", "run", "-ti", "--rm", "-v", "%s:/root" % d,
               environment, "bash", "script.sh"]

        start = time.time()
        self.log.info("Running cmd: %s" % " ".join(cmd))
        popen = subprocess.Popen(cmd, stdout=subprocess.PIPE)

        results = {
            "job": job,
            "output": [],
            "returncode": None
        }

        self.cache[job] = results
        for line in iter(popen.stdout.readline, b""):
            sys.stdout.write(line)
            results["output"].append(line)

        end = time.time()
        duration = end - start

        results["output"].append("")
        results["output"].append(
            "Job finished in %.2fs" % duration)
        results["returncode"] = popen.returncode
        results["duration"] = duration

        write_results(job)

    return job


def next_build(repo):
    """Compute next build number for `repo`

    Arguments:
        repo (str): user/repo combination, e.g. mottosso/pyblish-magenta

    """

    root = os.path.join(self.root, repo)
    builds = os.listdir(root)
    self.log.info("Getting next build (%s) from %s (%s)"
                  % (len(builds), root, builds))
    return len(builds) + 1


def write_results(job):
    """Persist results on disk

    Arguments:
        job (str): Name of job, e.g. pyblish/pyblish-magenta/1

    """

    results = self.cache[job]
    parent = os.path.dirname(job)
    parent_path = os.path.join(self.root, parent)

    self.log.info("Writing parent: %s" % parent_path)
    if not os.path.exists(parent_path):
        os.makedirs(parent)

    job_path = os.path.join(self.root, job)
    self.log.info("Finished, writing results to %s" % job_path)
    with open(job_path, "w") as f:
        json.dump(results, f)

    self.log.info("Done with script, sending new status 'success'")


def read_results(job):
    """Read results from disk

    Arguments:
        job (str): Name of job

    """

    if job not in self.cache:
        results = {}
        output_path = os.path.join(self.root, job)

        try:
            with open(output_path) as f:
                results = json.load(f)
        except:
            return None

        self.cache[job] = results

    return self.cache[job]


@app.route("/")
def home():
    return "<h3>Pyblish CI</h3>"


style = """
body {
    background-color: #fff;
    font-family: monospace;
    font-size: 12px;
    line-height: 16px;
    width: 800px;
    margin-left: auto;
    margin-right: auto;
    margin-top: 20px;
}

p {
    white-space: pre-wrap;
    margin: 0;
    padding: 0;
}

p a {
    width: 30px;
    text-align: right;
    color: #777;
}

span {
    display: inline-block;
    padding-left: 10px;
    letter-spacing: 0.4px;
    color: #F1F1F1;
}

a {
    display: inline-block;
    cursor: pointer;
}

p:hover {
    background-color: rgba(255, 255, 255, 0.05);
}

.log-body {
    background-color: #222;
    color: rgb(241, 241, 241);
    padding: 10px;
    border-radius: 5px;
}

.builds-body {
    display: flex;
    flex-direction: row;
    justify-content: flex-start;
    flex-wrap: wrap;
    width: 400px;
}

.builds-body a {
    width: 20px;
    height: 20px;
    font-size: 1.3em;

    text-align: center;
    padding: 5px;
    margin: 5px;
    background-color: #eee;
}
"""


@app.route("/jobs/<user>/<repo>")
def browse(user, repo):
    jobs = os.path.join(self.root, user, repo)
    self.log.info("Browsing: %s" % jobs)
    body = """
        <style>{style}</style>
        <h1>Builds for {name}</h1>
        <div class="builds-body">{output}</div>
    """

    builds = sorted(map(int, os.listdir(jobs)), key=int)

    return body.format(
        style=style,
        name=repo,
        output="\n".join("<a href=\"{0}/{1}\">{1}</a>".format(repo, build)
                         for build in builds))


@app.route("/jobs/<user>/<repo>/<build>")
def details(user, repo, build):

    job = "/".join([user, repo, build])
    if job not in self.pages:

        body = """
            <style>{style}</style>
            <h1>Results for {job}</h1>
            <div class="log-body">{output}</div>
        """

        results = read_results(job)

        if results is None:
            return "Job %s did not exist" % job

        lines = results["output"]
        if not isinstance(lines, list):
            lines = lines.split("\n")

        output = "\n".join(
            "<p><a>%s</a><span>%s</span></p>" % (
                lines.index(line) + 1, line) for line in lines)
        page = body.format(job=job, output=output, style=style)
        self.pages[job] = page

    return self.pages[job]


def request(action, path, **kwargs):
    """requests.get wrapper"""
    token = os.environ["GITHUB_API_TOKEN"]
    kwargs["headers"] = {"Authorization": "token %s" % token}
    return getattr(requests, action)(path, **kwargs)


class Handler(flask.ext.restful.Resource):
    def get(this):
        return "<p>This is where you'll point GitHub Events</p>"

    def post(self):
        headers = flask.request.headers
        payload = flask.request.json
        if headers.get("X-Github-Event") == "pull_request":
            if payload["action"] in ("opened", "synchronize"):
                return self.process_pull_request(payload["pull_request"])

    def process_pull_request(self, pull_request):
        repo = pull_request["base"]["repo"]["full_name"]
        sha = pull_request["head"]["sha"]

        endpoint = "https://api.github.com/repos/{repo}/statuses/{sha}"
        endpoint = endpoint.format(repo=repo, sha=sha)

        # E.g. pyblish/pyblish-magenta/24
        job = "%s/%s" % (repo, next_build(repo))

        self.create_status(endpoint, job, "pending")

        def worker():
            sys.modules[__name__].log.info("Running script..")
            try:
                run_script(job, pull_request)
                self.create_status(endpoint, job, "success")
            except:
                traceback.print_exc()
                self.create_status(endpoint, job, "failure")

        t = threading.Thread(target=worker)
        t.deamon = True
        t.start()

        sys.modules[__name__].log.info("Received pull request..")
        return "Pull request received!"

    def create_status(self, endpoint, job, status):
        descriptions = {
            "pending": "Working on it..",
            "success": "All good",
            "failure": "Things didn't go too well.."
        }

        r = request("post", endpoint, json={
            "state": status,
            # "target_url": "http://ci.pyblish.com/jobs/%s" % job,
            "target_url": "http://pyblish.ngrok.io/jobs/%s" % job,
            "description": descriptions[status],
            "context": "continuous-integration/travix"
        })

        if not r.status_code == 201:  # Created
            return False
        return True


api = flask.ext.restful.Api(app)
api.add_resource(Handler, "/handler")


if __name__ == '__main__':
    import argparse

    if "GITHUB_API_TOKEN" not in os.environ:
        self.log.info("GITHUB_API_TOKEN not set")
        sys.exit(1)

    parser = argparse.ArgumentParser()
    parser.add_argument("--port", default=8000, type=int)

    args = parser.parse_args()

    app.debug = True
    app.run(host='0.0.0.0', port=args.port)
