import os
import sys

import traceback
import threading

# Dependencies
import flask
import requests
import flask.ext.restful

import ci
import pyblish_ci

app = flask.Flask(__name__)

this = sys.modules[__name__]
this.app = app
this.pages = {}
this.log = pyblish_ci.log
this.root = "/ci"


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

.image-body {
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
    jobs = os.path.join(this.root, user, repo)
    this.log.info("Browsing: %s" % jobs)
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


def images(user, repo, build):

    job = "/".join([user, repo, build])

    results = ci.read_results(job)
    if not results:
        return "No results for job: %s" % job

    body = """
        <style>{style}</style>
        <h1>Images for {job}</h1>
        <div class="images-body">{output}</div>
    """

    images = results.keys()
    output = body.format(
        style=style,
        job=job,
        output="\n".join("<a href=\"{0}?image={1}\">{1}</a>".format(build,
                                                                    image)
                         for image in images))

    return body.format(job="test", output=output, style=style)


@app.route("/jobs/<user>/<repo>/<build>")
def image(user, repo, build):
    image = flask.request.args.get("image", None)
    if image is None:
        return images(user, repo, build)

    job = "/".join([user, repo, build])

    body = """
        <style>{style}</style>
        <h1>Results for {job}</h1>
        <div class="log-body">{output}</div>
    """

    results = ci.read_results(job).get(image)

    if results is None:
        return "Job %s did not exist" % job

    lines = results["output"]
    if not isinstance(lines, list):
        lines = lines.split("\n")

    output = "\n".join(
        "<p><a>%s</a><span>%s</span></p>" % (
            lines.index(line) + 1, line) for line in lines)
    return body.format(job=job, output=output, style=style)


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

        if headers.get("X-Github-Event") == "push":
            return self.process_push(payload)

    def process_pull_request(self, pull_request):
        """Handle incoming pull-request event"""
        repo = pull_request["base"]["repo"]["full_name"]
        sha = pull_request["head"]["sha"]
        url = pull_request["base"]["repo"]["clone_url"]
        number = pull_request["number"]
        branch = "pull/%s/head" % number

        self.process_event(repo, url, branch, sha)

    def process_push(self, push):
        """Handle incoming push event"""
        repo = push["repository"]["full_name"]
        url = push["repository"]["clone_url"]
        sha = push["after"]
        branch = "master"

        self.process_event(repo, url, branch, sha)

    def process_event(self, repo, url, branch, sha):
        endpoint = "https://api.github.com/repos/{repo}/statuses/{sha}"
        endpoint = endpoint.format(repo=repo, sha=sha)

        # E.g. pyblish/pyblish-magenta/24
        job = "%s/%s" % (repo, ci.next_build(repo))

        self.create_status(endpoint, job, "pending")

        def worker():
            this.log.info("Running script..")

            success = False
            try:
                builds = ci.run_job(job, url, branch)
                this.log.info("Builds complete, evaluating results..")
                for build in builds:
                    this.log.info(build)
                    if build["results"]["success"]:
                        success = True
            except:
                traceback.print_exc()

            if success:
                self.create_status(endpoint, job, "success")
            else:
                self.create_status(endpoint, job, "failure")

        t = threading.Thread(target=worker)
        t.deamon = True
        t.start()

        this.log.info("Received pull request..")
        return "Pull request received!"

    def create_status(self, endpoint, job, status):
        descriptions = {
            "pending": "Working on it..",
            "success": "All good",
            "failure": "Things didn't go too well.."
        }

        r = request("post", endpoint, json={
            "state": status,
            "target_url": "http://ci.pyblish.com/jobs/%s" % job,
            "description": descriptions[status],
            "context": "continuous-integration/travix"
        })

        if not r.status_code == 201:  # Created
            return False
        return True


api = flask.ext.restful.Api(app)
api.add_resource(Handler, "/handler")
