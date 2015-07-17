import os
import sys
import json
import time
import Queue
import shutil
import tempfile
import contextlib
import subprocess

# Dependencies
import yaml

# Local library
import pyblish_ci

this = sys.modules[__name__]
this.cache = {}
this.root = "/ci"
this.job_queue = Queue.Queue()
this.temp_queue = Queue.Queue()
this.log = pyblish_ci.log


def cleaner():
    """Permanently erase temporary directories used during processing

    A temporary directory is created per job, this cleaner will
    ensure that these directories are deleted afterwards, but
    not too close to the completion of Docker due to file locking.

    """

    while True:
        tempdir = this.temp_queue.get()

        count = 0
        while True:
            try:
                time.sleep(10)
                this.log.info("Cleaning up %s" % tempdir)
                shutil.rmtree(tempdir)
                break
            except OSError:
                this.log.info("Failed..")
                count += 1
                if count > 2:
                    break
                this.log.info("Retrying in 10 seconds..")

        this.temp_queue.task_done()


def worker():
    """Run jobs form queue

    Run only a single job at a time.

    A job contains one or more "builds",
    and can look like this:
        [
            {"job": "...", "script": [...]},
            {"job": "...", "script": [...]},
            {"job": "...", "script": [...]}
        ]

    """

    while True:
        build_queue = this.job_queue.get()

        # Process each build in queue
        while True:
            try:
                build = build_queue.get_nowait()
                this.log.info("Running build for: {job}".format(**build))
                results = run_build(build)
                build["results"].update(results)
                build_queue.task_done()
            except Queue.Empty:
                break

        this.job_queue.task_done()


@contextlib.contextmanager
def tempdir():
    tempdir = tempfile.mkdtemp()
    try:
        yield tempdir
    finally:
        this.temp_queue.put(tempdir)


def run_build(build):
    """Run script in image

    Arguments:
        build (dict): The current build, e.g.
            {
                "job": pyblish/pyblish-magenta/1,
                "image": "mottosso/maya:2016sp1",
                "script": ["echo Hello World"],
                "root": "/tmp/tmpWXsd35"
            }

    Example:
        >>> build(["echo hello"], "mottosso/maya")

    Returns:
        A `results` dictionary, with the following:
        {
            "job": Current job,
            "output": Full output from Docker image,
            "returncode": Return code of script,
            "success": True if return code was 0, else False
        }

    """

    job = build["job"]
    image = build["image"]
    script = build["script"]
    root = build["root"]

    # Initialise virtual machine
    script_sh = os.path.join(root, "script.sh")
    if not os.path.exists(script_sh):
        script.insert(0, ". ~/.bashrc")
        script.insert(1, "shopt -s expand_aliases")

        # Duplicate source repository
        script.insert(2, "echo Copying context")
        script.insert(3, "cp -rf /citmp/* /root")

        # Convert .pyblish:script to script.sh
        with open(script_sh, "w") as f:
            f.write("\n".join(script))

    this.log.info("Running script:\n%s" % script)
    cmd = [
        "docker", "run", "-t", "--rm",
        "-v", "%s:/citmp" % root,
        image, "bash", "/citmp/script.sh"
    ]

    __start = time.time()
    this.log.info("Running cmd: %s" % " ".join(cmd))
    popen = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        env=dict(os.environ, **{"BASH_ENV": "~/bashrc"}))

    results = {
        "job": job,
        "output": [],
        "returncode": None,
        "success": None
    }

    if job not in this.cache:
        this.cache[job] = {}

    this.cache[job][image] = results

    # Write results to cache as it is running
    for line in iter(popen.stdout.readline, b""):
        sys.stdout.write(line)
        results["output"].append(line)
    popen.communicate()  # Wait to finish

    duration = time.time() - __start

    results["output"].append("")
    results["output"].append(
        "Job finished in %.2fs" % duration)
    results["returncode"] = popen.returncode
    results["success"] = False if popen.returncode else True
    results["duration"] = duration

    write_results(job)

    return results


def run_job(job, url, branch=None):
    """Post a new job to queue, from .pyblish configuration

    Arguments:
        job (str): Path of job, e.g. pyblish/pyblish-magenta/1
        pull_request (dict): GitHub pull request object

    """

    this.log.info("Running tests..")

    with tempdir() as d:
        subprocess.check_output(
            ["git", "clone", url, d])
        subprocess.check_output(
            ["git", "fetch", "origin", branch + ":current"], cwd=d)
        subprocess.check_output(
            ["git", "checkout", "current"], cwd=d)

        this.log.info("Running job: %s" % job)

        # Parse configuration
        config = os.path.join(d, ".pyblish")
        if not os.path.exists(config):
            return this.log.error("No .pyblish file found")

        with open(config) as f:
            config = yaml.load(f)

        # Get image
        images = config.get("image")
        if not images:
            return this.log.error("No image specified")

        if isinstance(images, basestring):
            images = [images]

        # Remove duplicates
        seen = set()
        images = [x for x in images if x not in seen and not seen.add(x)]

        script = config.get("script")
        if not script:
            return this.log.error("No script specified")
        if isinstance(script, basestring):
            script = [script]

        # Form a tree of tasks, e.g.
        # job
        # - build1
        # - build2
        # - build3
        builds = list()
        for image in images:
            build = {
                "job": job,
                "script": script,
                "image": image,
                "root": d,
                "results": {}
            }
            builds.append(build)

        # Add job to queue
        build_queue = Queue.Queue()
        map(build_queue.put, builds)
        this.job_queue.put(build_queue)

        this.log.info("Awaiting build_queue..")
        build_queue.join()
        this.log.info("build_queue finished")

        return builds


def next_build(repo):
    """Compute next build number for `repo`

    Arguments:
        repo (str): user/repo combination, e.g. mottosso/pyblish-magenta

    """

    root = os.path.join(this.root, repo)

    try:
        builds = os.listdir(root)
    except OSError:
        return 1

    this.log.info("Getting next build (%s) from %s (%s)"
                  % (len(builds), root, builds))
    return len(builds) + 1


def write_results(job):
    """Persist results on disk

    Arguments:
        job (str): Name of job, e.g. pyblish/pyblish-magenta/1

    """

    results = this.cache[job]
    this.log.debug("Writing results:\n%s" % json.dumps(results, indent=4))

    parent = os.path.dirname(job)
    parent_path = os.path.join(this.root, parent)

    this.log.info("Writing parent: %s" % parent_path)
    if not os.path.exists(parent_path):
        os.makedirs(parent_path)

    job_path = os.path.join(this.root, job)
    this.log.info("Finished, writing results to %s" % job_path)
    with open(job_path, "w") as f:
        json.dump(results, f)

    this.log.info("Done with script, sending new status 'success'")


def read_results(job):
    """Read results from disk

    Arguments:
        job (str): Name of job

    """

    if job not in this.cache:
        results = {}
        output_path = os.path.join(this.root, job)

        try:
            with open(output_path) as f:
                results = json.load(f)
        except:
            return None

        this.cache[job] = results

    return this.cache[job]
