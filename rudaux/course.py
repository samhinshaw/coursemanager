# This will be called by cron and run after assignment closes

import requests
import os
import sys
import re
import shutil
import textwrap
import rudaux
from pathlib import Path
# Import my custom utiliy functions
# For parsing assignments from CSV
# import pandas as pd
# For progress bar
from tqdm import tqdm
from weir import zfs
from github import Github
from git import Repo
# For setting up autograding
from crontab import CronTab
from dateutil.parser import parse
from terminaltables import AsciiTable, SingleTable
# For decoding base64-encoded files from GitHub API
from base64 import b64decode
# for urlencoding query strings to persist through user-redirect
import urllib.parse as urlparse
# Bring in nbgrader API
from nbgrader.apps import NbGraderAPI
# Don't reinvent the wheel! Use nbgrader's method for finding notebooks
from nbgrader import utils as nbutils
# Bring in Traitlet Configuration methods
from traitlets.config import Config
from traitlets.config.application import Application
from rudaux import utils
import rudaux

#* All internal _methods() return an object

#* All external  methods() return self (are chainable), and mutate object state
#* or initiate other side-effects

# Must be instantiated with a course ID
class Course:
  """
  Course object for manipulating an entire Canvas/JupyterHub/nbgrader course
  """

  def __init__(
    self, 
    course_dir=None,
    cron=False
  ):
    """
    :param course_dir: The directory your course. If none, defaults to current working directory. 
    :type course_dir: str

    :returns: A Course object for performing operations on an entire course at once.
    :rtype: Course
    """

    #=======================================#
    #     Working Directory & Git Sync      #
    #=======================================#

    # Set up the working directory. If no course_dir has been specified, then it
    # is assumed that this is the course directory. 
    self.working_directory = course_dir if course_dir is not None else os.getcwd()
    
    repo = Repo(self.working_directory)

    # Before we do ANYTHING, make sure our working directory is clean with no
    # untracked files! Unless we're running a cron job, in which case we don't
    # want to fail for an unexpected reason.
    if (repo.is_dirty() or repo.untracked_files) and (not cron):
      continue_with_dirty = input(
        """
        Your repository is currently in a dirty state (modifications or
        untracked changes are present). We strongly suggest that you resolve
        these before proceeding. Continue? [y/n]:"""
      )
      # if they didn't say no, exit
      if continue_with_dirty.lower() != 'y':
        sys.exit("Exiting...")

    print("Initializing course and pulling remote copy of instructors repository...")

    # pull the latest copy of the repo
    utils.pull_repo(repo_dir=self.working_directory)

    # Make sure we're running our nbgrader commands within our instructors repo.
    # this will contain our gradebook database, our source directory, and other
    # things.
    config = Config()
    config.CourseDirectory.root = self.working_directory

    #=======================================#
    #              Load Config              #
    #=======================================#

    # Check for an nbgrader config file...
    if not os.path.exists(os.path.join(self.working_directory, 'nbgrader_config.py')):
      # if there isn't one, make sure there's at least a rudaux config file
      if not os.path.exists(os.path.join(self.working_directory, 'rudaux_config.py')):
        sys.exit(
          """
          You do not have nbgrader_config.py or rudaux_config.py in your current
          directory. We need at least one of these to set up your course
          parameters! You can specify a directory with the course_dir argument
          if you wish.
          """
        )

    # use the traitlets Application class directly to load nbgrader config file.
    # reference:
    # https://github.com/jupyter/nbgrader/blob/41f52873c690af716c796a6003d861e493d45fea/nbgrader/server_extensions/validate_assignment/handlers.py#L35-L37

    # ._load_config_files() returns a generator, so if the config is missing,
    # the generator will act similarly to an empty array

    # load rudaux_config if it exists, otherwise just bring in nbgrader_config.
    for rudaux_config in Application._load_config_files('rudaux_config', path=self.working_directory):
      config.merge(rudaux_config)
    
    for nbgrader_config in Application._load_config_files('nbgrader_config', path=self.working_directory):
      config.merge(nbgrader_config)

    #=======================================#
    #           Set Config Params           #
    #=======================================#

    ## CANVAS PARAMS

    # Before we continue, make sure we have all of the necessary parameters.
    self.course_id = config.get('Canvas', {}).get('course_id')
    self.canvas_url = config.get('Canvas', {}).get('canvas_url')
    self.external_tool_name = config.get('Canvas', {}).get('external_tool_name')
    self.external_tool_level = config.get('Canvas', {}).get('external_tool_level')
    # The canvas url should have no trailing slash
    self.canvas_url = re.sub(r"/$", "", self.canvas_url)

    ## GITHUB PARAMS
    self.stu_repo_url = config.get('GitHub', {}).get('stu_repo_url', '')
    self.assignment_release_path = config.get('GitHub', {}).get('assignment_release_path')
    self.ins_repo_url = config.get('GitHub', {}).get('ins_repo_url')
    # subpath not currently supported
    # self.ins_dir_subpath = config.get('GitHub').get('ins_dir_subpath')
      
    ## JUPYTERHUB PARAMS

    self.hub_url = config.get('JupyterHub', {}).get('hub_url')
    # The hub url should have no trailing slash
    self.hub_url = re.sub(r"/$", "", self.hub_url)
    # Note hub_prefix, not base_url, to avoid any ambiguity
    self.hub_prefix = config.get('JupyterHub', {}).get('base_url')
    # If prefix was set, make sure it has no trailing slash, but a preceding
    # slash
    if self.hub_prefix is not None:
      self.hub_prefix = re.sub(r"/$", "", self.hub_prefix)
      if re.search(r"^/", self.hub_prefix) is None:
        self.hub_prefix = fr"/{self.hub_prefix}"

    ## COURSE PARAMS

    self.tmp_dir = config.get('Course', {}).get('tmp_dir')
    assignment_list = config.get('Course', {}).get('assignments')

    ## Repurpose the rest of the params for later batches
    ## (Hang onto them in case we need something)

    self._full_config = config

    #=======================================#
    #        Validate URLs (Slightly)       #
    #=======================================#

    urls = {
      'JupyterHub.hub_url': self.hub_url,
      'Canvas.canvas_url': self.canvas_url
    }

    for key, value in urls.items():
      if re.search(r"^https{0,1}", value) is None:
        sys.exit(
          f"""
          You must specify the scheme (e.g. https://) for all URLs.
          You are missing the scheme in "{key}":
          {value}
          """
        )
      if re.search(r".git$", value) is not None:
        sys.exit(
          f"""
          Please do not use .git-appended URLs. 
          You have used a .git url in "{key}":
          {value}
          """
        )

      self.stu_git_url = self.stu_repo_url
      self.ins_git_url = self.ins_repo_url

    #=======================================#
    #       Check For Required Params       #
    #=======================================#

    # Finally, before we continue, make sure all of our required parameters were
    # specified in the config file(s)
    required_params = {
      "Canvas.course_id": self.course_id,
      "Canvas.canvas_url": self.canvas_url,
      "GitHub.stu_repo_url": self.stu_repo_url,
      "GitHub.ins_repo_url": self.ins_repo_url,
      "JupyterHub.hub_url": self.hub_url,
      "Course.assignments": assignment_list
    }
    
    # If any are none...
    if None in required_params.values():
      # Figure out which ones are none and let the user know.
      for key, value in required_params.items():
        if value is None:
          print(f"\"{key}\" is missing.")
      sys.exit('Please make sure you have specified all required parameters in your config file.')

    #=======================================#
    #       Check For Optional Params       #
    #=======================================#

    # Now look for all of our optional parameters. If any are missing, let the
    # user know we'll be using the default. 
    optional_params = {
      "assignment_release_path": {
        "value": self.assignment_release_path,
        "default": 'materials',
        "config_name": "GitHub.assignment_release_path"
      },
      # "assignment_source_path": {
      #   "value": self.assignment_source_path,
      #   "default": "source",
      #   "config_name": "c.GitHub.assignment_source_path"
      # },
      "hub_prefix": {
        "value": self.hub_prefix,
        "default": "",
        "config_name": "JupyterHub.base_url"
      },
      "tmp_dir": {
        "value": self.tmp_dir,
        "default": os.path.join(Path.home(), 'tmp'),
        "config_name": "Course.tmp_dir"
      },
      "external_tool_name": {
        "value": self.external_tool_name,
        "default": 'Jupyter',
        "config_name": "Canvas.external_tool_name"
      },
      "external_tool_level": {
        "value": self.external_tool_level,
        "default": 'course',
        "config_name": "Canvas.external_tool_level"
      }
    }

    for key, param in optional_params.items():
      if param.get('value') is None:
        setattr(self, key, param.get('default'))
        print(f"\"{param.get('config_name')}\" is missing, using default parameter of \"{getattr(self, key)}\"")

    # Make sure no preceding or trailing slashes in assignment release path
    self.assignment_release_path = re.sub(r"/$", "", self.assignment_release_path)
    self.assignment_release_path = re.sub(r"^/", "", self.assignment_release_path)

    # check for tokens
    # github_token_name = config.get('GitHub').get('github_token_name')
    # token_names = config.get('GitHub').get('token_names')
        
    # #=======================================#
    # #           Set GitHub Token            #
    # #=======================================#

    # # If the config file specifies multiple tokens, use those
    # if token_names is not None:
    #   # instantiate our token object as empty
    #   self.tokens = []
    #   # Then for each token specified...
    #   for token_name in token_names:
    #     # Create a token object with the domain and token necessary
    #     self.tokens.append({
    #       "domain": token_name.get('domain'),
    #       "token": self._get_token(token_name.get('token_name'))
    #     })
    # # otherwise we'll use the same token for both github domains
    # else:
    #   if github_token_name is None:
    #     self.github_tokens = None
    #     # Only look for PAT if it is specified by the user
    #     # This way we can prompt for user/pass if no token is available
    #       # if the user didn't specify a token_name, use 'GITHUB_PAT'
    #       # print("Searching for default GitHub token, GITHUB_PAT...")
    #       # github_token_name = 'GITHUB_PAT'
    #   else: 
    #     # get the single token we'll be using
    #     token = self._get_token(github_token_name)

    #     # Then specify that we're using the same token for each domain
    #     self.github_tokens = [
    #       {
    #         "domain": urlparse.urlsplit(self.stu_repo_url).netloc,
    #         "token": token
    #       }, 
    #       {
    #         "domain": urlparse.urlsplit(self.ins_repo_url).netloc,
    #         "token": token
    #       }
    #     ]

    #=======================================#
    #           Set Canvas Token            #
    #=======================================#

    canvas_token_name = config.get('Canvas').get('token_name')

    if canvas_token_name is None:
      print("Searching for default Canvas token, CANVAS_TOKEN...")
      canvas_token_name = 'CANVAS_TOKEN'

    self.canvas_token = self._get_token(canvas_token_name)

    #=======================================#
    #        Finalize Setting Params        #
    #=======================================#

    # set up the nbgrader api with our merged config files
    self.nb_api = NbGraderAPI(config=config)

    # assign init params to object
    # self.canvas_token = self._get_token(canvas_token_name)
    # self.course = self._get_course()

    # Set crontab
    self.cron = CronTab(user=True)

    #=======================================#
    #        Instantiate Assignments        #
    #=======================================#

    # Subclass assignment for this course:
    class CourseAssignment(rudaux.Assignment):
      course = self

    instantiated_assignments = []

    for _assignment in assignment_list:
      assignment = CourseAssignment(
        name=_assignment.get('name'),
        duedate=_assignment.get('duedate'),
        # default to 0 points
        points=_assignment.get('points', 0),
        status='unknown'
      )
      instantiated_assignments.append(assignment)

    self.assignments = instantiated_assignments

#------------------ End Constructor ------------------#

  # Get the canvas token from the environment
  @staticmethod
  def _get_token(token_name: str):
    """
    Get an API token from an environment variable.
    """
    try:
      token = os.environ[token_name]
      return token
    except KeyError as e:
      print(f"Could not find the environment variable \"{token_name}\".")
      raise e

  def get_external_tool_id(self) -> 'Course': 
    """Find the ID of the external tool created in Canvas that represents your JupyterHub server."""

    print("Looking for the external tool in Canvas...")

    resp = requests.get(
      url=urlparse.urljoin(
        self.canvas_url, f"/api/v1/{self.external_tool_level}s/{self.course_id}/external_tools"
      ),
      headers={
        "Authorization": f"Bearer {self.canvas_token}",
        "Accept": "application/json+canvas-string-ids"
      },
      params={
        "search_term": self.external_tool_name
      }
    )
    # first make sure we didn't silently error
    resp.raise_for_status()

    external_tools = resp.json()
    external_tool = external_tools[0]

    if not external_tools:
      sys.exit(
        f"""
        No external tools found with the name {self.external_tool_name}"
        at the {self.external_tool_level} level. 
        Exiting...
        """
      )
    elif len(external_tools) > 1:
      print(
        f"""
        More than one external tool found, using the one named "{external_tool.get('name')}".
        Description: "{external_tool.get('description')}"
        """
      )

    self.external_tool_id = external_tool.get('id')

    return self

  def get_students_from_canvas(self) -> 'Course':
    """
    Get the student list for a course. 
    """

    print('Querying list of students from Canvas...')
    # List all of the students in the course
    resp = requests.get(
      url=f"{self.canvas_url}/api/v1/courses/{self.course_id}/users",
      headers={
        "Authorization": f"Bearer {self.canvas_token}",
        "Accept": "application/json+canvas-string-ids"
      },
      json={
        "enrollment_type": ["student"]
      },
    )

    # Make sure our request didn't fail silently
    resp.raise_for_status()

    # pull out the response JSON
    students = resp.json()

    self.students = students
    return self

  # def get_assignments_from_canvas(self):
  #   """
  #   Get all assignments for a course.
  #   """
  #   resp = requests.get(
  #     url=f"{self.canvas_url}/api/v1/courses/{self.course_id}/assignments",
  #     headers={
  #       "Authorization": f"Bearer {self.canvas_token}",
  #       "Accept": "application/json+canvas-string-ids"
  #     }
  #   )

  #   # Make sure our request didn't fail silently
  #   resp.raise_for_status()

  #   # pull out the response JSON
  #   canvas_assignments = resp.json()

  #   # Create an assignment object from each assignment
  #   # `canvas_assignments` is a list of objects (dicts) so `**` is like the object spread operator (`...` in JS)
  #   assignments = map(
  #     lambda assignment: Assignment(**assignment),
  #     canvas_assignments
  #   )
  #   self.assignments = assignments

  #   return self

  def sync_nbgrader(self):
    """
    Enter information into the nbgrader gradebook database about the assignments and the students.
    """

    print("Syncing your list of students and assignments with nbgrader...")

    # nbgrader API docs: 
    # https://nbgrader.readthedocs.io/en/stable/api/gradebook.html#nbgrader.api.Gradebook
    gradebook = self.nb_api.gradebook

    try:
      ###############################
      #     Update Student List     #
      ###############################

      nb_student_ids = list(map(lambda _student: _student.id, gradebook.students))
      # Don't use .get('id') here, because we want this to fail loudly
      canvas_student_ids = list(map(lambda _student: _student['id'], self.students))

      # First find the students that are in canvas but NOT in nbgrader
      students_missing_from_nbgrader = list(set(canvas_student_ids) - set(nb_student_ids))
      # Then find the students that are in nbgrader but NOT in Canvas (this
      # could only happen if they had withdrawn from the course)
      students_withdrawn_from_course = list(set(nb_student_ids) - set(canvas_student_ids))

      students_no_change = set(canvas_student_ids).union(set(nb_student_ids))

      student_status = [
        ["Student ID", "Status", "Action"]
      ]

      if students_missing_from_nbgrader:
        for student_id in students_missing_from_nbgrader:
          student_status.append(
            [student_id, "missing from nbgrader", "added to nbgrader"]
          )
          gradebook.add_student(student_id)
      if students_withdrawn_from_course:
        for student_id in students_withdrawn_from_course:
          gradebook.remove_student(student_id)
          student_status.append(
            [student_id, "missing from canvas", "removed from nbgrader"]
          )
      if students_no_change:
        for student_id in students_no_change:
          student_status.append(
            [student_id, "present in both", " - "]
          )

      table = SingleTable(student_status)
      table.title = 'Students'
      print("\n")
      print(table.table)

      ##################################
      #     Update Assignment List     #
      ##################################

      nb_assignments = list(map(lambda _assignment: _assignment.name, gradebook.assignments))
      config_assignments = list(map(lambda _assignment: _assignment.name, self.assignments))

      assignments_missing_from_nbgrader = list(set(config_assignments) - set(nb_assignments))
      assignments_withdrawn_from_config = list(set(nb_assignments) - set(config_assignments))

      assignments_no_change = set(config_assignments).union(set(nb_assignments))

      assignment_status = [
        ["Assignment", "Status", "Action"]
      ]

      if assignments_missing_from_nbgrader:
        print("Assignments missing from nbgrader, adding...")
        for assignment_name in assignments_missing_from_nbgrader:
          gradebook.add_assignment(assignment_name)
          assignment_status.append(
            [assignment_name, "missing from nbgrader", "added to nbgrader"]
          )
      if assignments_withdrawn_from_config:
        for assignment_name in assignments_withdrawn_from_config:
          gradebook.remove_assignment(assignment_name)
          assignment_status.append(
            [assignment_name, "missing from config", "removed from nbgrader"]
          )
      if assignments_no_change:
        for assignment_name in assignments_no_change:
          assignment_status.append(
            [assignment_name, "present in both", " - "]
          )

      table = SingleTable(assignment_status)
      table.title = 'Assignments'
      print("\n")
      print(table.table)
      print("\n")

    # Always make sure we close the gradebook connection, even if we error
    except Exception as e:
      print("An error occurred, closing connection to gradebook...")
      gradebook.close()
      raise e


    print("Closing connection to gradebook...")
    gradebook.close()
    return self

  def assign_all(
    self, 
    assignments=None,
    overwrite=False,
  ):
    """Assign assignments for a course
    
    :param assignments: The name(s) of the assignment(s) you wish to assign, as a string or list of strings.
    :type assignments: str, List[str]
    :param overwrite: Whether or not you wish to overwrite preexisting directories
    :type overwrite: bool
    """

    # Make sure we've got assignments to assign
    if not assignments:
      print("No assignment in particular specified, assigning all assignments...")
      assignments_to_assign = self.assignments
    # otherwise, match the assignment(s) provided to the one in our config file
    else:
      # if just one assignment was specified...
      if isinstance(assignments, str):
        # find the assignments which match that name
        assignments_to_assign = list(filter(lambda assn: assn.name == assignments, self.assignments))
      elif isinstance(assignments, list):
        assignments_to_assign = []
        for assignment in assignments:
          match = list(filter(lambda assn: assn.name == assignments, self.assignments))
          assignments_to_assign.append(match)
      else: 
        sys.exit('Invalid argument supplied to `assign()`. Please specify a string or list of strings to assign.')

    # First things first, make the temporary student directory. No need to clone
    # the instructors' dir since we're working in that already, and when we
    # initialize the course we do a git pull to make sure we have the latest
    # copy. 
    stu_repo_dir = os.path.join(self.tmp_dir, 'students')

    #=======================================#
    #                Clone Repo             #
    #=======================================#

    try:
      utils.clone_repo(self.stu_repo_url, stu_repo_dir, overwrite)
    except Exception as e:
      print("There was an error cloning your student repository")
      raise e

    #=======================================#
    #          Make Student Version         #
    #=======================================#

    # set up array to save assigned assignment names in. This will be so we can
    # record which assignments were assigned in the commit message. 
    assignment_names = []

    ### FOR LOOP - ASSIGN ASSIGNMENTS ###

    # For each assignment, assign!!
    print('Creating student versions of assignments with nbgrader...')
    for assignment in tqdm(assignments_to_assign): 

      # Push the name to our names array for adding to the commit message.
      assignment_names.append(assignment.name)

      # assign the given assignment!
      self.nb_api.assign(assignment.name)

    ### END LOOP ###

    print("Copying student versions to your student repository...")

    # set up directories to copy the assigned assignments to
    generated_assignment_dir = os.path.join(self.working_directory, 'release')
    student_assignment_dir = os.path.join(stu_repo_dir, self.assignment_release_path)

    #========================================#
    #   Move Assignments to Students Repo    #
    #========================================#

    if os.path.exists(student_assignment_dir):
      utils.safely_delete(student_assignment_dir, overwrite)

    # Finally, copy to the directory, as we've removed any preexisting ones or
    # exited if we didn't want to
    shutil.copytree(generated_assignment_dir, student_assignment_dir)

    print("Committing changes in your instructor and student repositories...")

    #===========================================#
    # Commit & Push Changes to Instructors Repo #
    #===========================================#

    utils.commit_repo(self.working_directory, assignment_names)
    utils.push_repo(self.working_directory)

    #========================================#
    # Commit & Push Changes to Students Repo #
    #========================================#

    utils.commit_repo(stu_repo_dir, assignment_names)
    utils.push_repo(stu_repo_dir)

    return self

  # def get_assignments_from_csv(self, path: str):
  #   """
  #   Bring in assignments from a CSV file. 
  #   CSV file should contain the following columns: 
    
  #   [required]
  #   - name (str): the name of the assignment
  #   - duedate (str): due date for the assignment
  #   - notebook_path (str): github URL at which the jupyter notebook lives
  #   - points_possible (int): the number of possible points

  #   [optional]
  #   - published (bool): whether the assignment should be published
  #   - description (str): a description of the assignment
  #   - unlock_at (str): a date at which the assignment becomes available
  #   - lock_at (str): date after the due date to which students can submit their assignment for partial credit

  #   :param path: Path to the CSV file. 
  #   """
  #   assignments = pd.read_csv(path)
  #   print(assignments)

  def create_canvas_assignments(self) -> 'None':
    """Create assignments for a course.
    """

    print("Creating assignments (preexisting assignments with the same name will be updated)...")

    # Initialize status table that we can push to as we go
    assignment_status = [
      ['Assignment', 'Action']
    ]

    for assignment in tqdm(self.assignments):
      status = assignment.update_or_create_canvas_assignment()
      assignment_status.append([assignment.name, status])

    ## Print status for reporting:
    table = SingleTable(assignment_status)
    table.title = 'Assignments'
    print("\n")
    print(table.table)
    print("\n")
    return self

  # def schedule_grading(self):
  #   """
  #   Schedule assignment grading tasks in crontab. 
  #   It would probably make more sense to use `at` instead of `cron` except that:
  #     1. CentOS has `cron` by default, but not `at`
  #     2. The python CronTab module exists to make this process quite easy.
  #   """
  #   # If there is no 'lock at' time, then the due date is the time to grade.
  #   # Otherwise, grade at the 'lock at' time. This is to allow partial credit
  #   # for late assignments.
  #   # Reference: https://community.canvaslms.com/docs/DOC-10327-415273044
  #   for assignment in tqdm(self.assignments):
  #     self._schedule_assignment_grading(assignment)
  #   print('Grading scheduled!')

  # def _schedule_assignment_grading(self, assignment):
  #   job = self.cron.new(
  #     command=f"nbgrader collect {assignment.get('name')}",
  #     comment=f"Autograde {assignment.get('name')}"
  #   )

  #   if assignment.get('lock_at') is not None:
  #     close_time = parse(assignment['lock_at'])

  #   elif assignment.get('due_at') is not None:
  #     close_time = parse(assignment['due_at'])

  #   # Will need course info here
  #   elif self.course.get('end_at') is not None:
  #     close_time = parse(self.course['end_at'])

  #   else:
  #     close_time: None
  #     print(
  #       'Could not find an end date for your course in Canvas, automatic grading will not be scheduled.'
  #     )

  #   # * Make sure we don't have a job for this already, and then set it if it's valid
  #   existing_jobs = self.cron.find_command(f"nbgrader collect {assignment.get('name')}")

  #   # wonky syntax because find_command & find_comment return *generators*
  #   if (len(list(existing_jobs)) > 0) & job.is_valid():
  #     # Set job
  #     job.setall(close_time)
  #     self.cron.write()
  #   else:
  #     # delete previous command here
  #     # then set job
  #     job.setall(close_time)
  #     self.cron.write()

  # def _get_course(self):
  #   """
  #   Get the basic course information from Canvas
  #   """
  #   resp = requests.get(
  #     url=f"{self.canvas_url}/api/v1/courses/{self.course_id}",
  #     headers={
  #       "Authorization": f"Bearer {self.canvas_token}",
  #       "Accept": "application/json+canvas-string-ids"
  #     }
  #   )

  #   # Make sure our request didn't fail silently
  #   resp.raise_for_status()
    
  #   # pull out the response JSON
  #   course = resp.json()
  #   return course
