import logging
import os
from copy import deepcopy, copy
from enum import Enum, auto
from subprocess import run, PIPE, DEVNULL
from typing import Sequence, List, Tuple, Set, Union, Iterable

from aurman.aur_utilities import is_devel, get_aur_info
from aurman.colors import Colors, color_string
from aurman.own_exceptions import InvalidInput, ConnectionProblem
from aurman.utilities import strip_versioning_from_name, split_name_with_versioning, version_comparison, ask_user
from aurman.wrappers import expac, makepkg, pacman


class PossibleTypes(Enum):
    """
    Enum containing the possible types of packages
    """
    REPO_PACKAGE = auto()
    AUR_PACKAGE = auto()
    DEVEL_PACKAGE = auto()
    PACKAGE_NOT_REPO_NOT_AUR = auto()


class DepAlgoSolution:
    """
    Class used to track solutions while solving the dependency problem
    """

    def __init__(self, packages_in_solution, visited_packages, visited_names):
        self.packages_in_solution: List['Package'] = packages_in_solution
        self.visited_packages: List['Package'] = visited_packages
        self.visited_names: Set[str] = visited_names
        self.is_valid: bool = True  # may be set to False by the algorithm in case of conflicts, dep-cycles, ...


class DepAlgoFoundProblems:
    """
    Base class for the possible problems which may occur during solving the dependency problem
    """

    def get_relevant_packages(self) -> Iterable['Package']:
        return []


class DepAlgoCycle(DepAlgoFoundProblems):
    """
    Problem class for dependency cycles
    """

    def __init__(self, cycle_packages):
        self.cycle_packages: List['Package'] = cycle_packages

    def __repr__(self):
        return "Dep cycle: " + " -> ".join(
            [color_string((Colors.LIGHT_MAGENTA, str(package))) for package in self.cycle_packages])

    def __eq__(self, other):
        return isinstance(other, self.__class__) and tuple(self.cycle_packages) == tuple(other.cycle_packages)

    def __hash__(self):
        return hash(tuple(self.cycle_packages))

    def get_relevant_packages(self):
        return self.cycle_packages


class DepAlgoConflict(DepAlgoFoundProblems):
    """
    Problem class for conflicts
    """

    def __init__(self, conflicting_packages, way_to_conflict):
        self.conflicting_packages: Set['Package'] = conflicting_packages
        self.way_to_conflict: List['Package'] = way_to_conflict

    def __repr__(self):
        return "Conflicts between: " + ", ".join([color_string((Colors.LIGHT_MAGENTA, str(package))) for package in
                                                  self.conflicting_packages]) + "\nWay to conflict: " + " -> ".join(
            [color_string((Colors.LIGHT_MAGENTA, str(package))) for package in self.way_to_conflict])

    def __eq__(self, other):
        return isinstance(other, self.__class__) and frozenset(self.conflicting_packages) == frozenset(
            other.conflicting_packages) and tuple(self.way_to_conflict) == tuple(other.way_to_conflict)

    def __hash__(self):
        return hash((frozenset(self.conflicting_packages), tuple(self.way_to_conflict)))

    def get_relevant_packages(self):
        return self.conflicting_packages


class DepAlgoNotProvided(DepAlgoFoundProblems):
    """
    Problem class for dependencies without at least one provider
    """

    def __init__(self, dep_not_provided, package):
        self.dep_not_provided: str = dep_not_provided
        self.package: 'Package' = package

    def __repr__(self):
        return "Not provided: {} but needed by {}".format(
            color_string((Colors.LIGHT_MAGENTA, str(self.dep_not_provided))),
            color_string((Colors.LIGHT_MAGENTA, str(self.package))))

    def __eq__(self, other):
        return isinstance(other,
                          self.__class__) and self.dep_not_provided == other.dep_not_provided and self.package == other.package

    def __hash__(self):
        return hash((self.dep_not_provided, self.package))

    def get_relevant_packages(self):
        return [self.package]


class Package:
    """
    Class representing Arch Linux packages
    """
    # default editor path
    default_editor_path = os.environ.get("EDITOR", os.path.join("usr", "bin", "nano"))
    # directory of the cache
    cache_dir = os.path.join(os.environ.get("XDG_CACHE_HOME", os.path.expanduser(os.path.join("~", ".cache"))),
                             "aurman")

    @staticmethod
    def user_input_to_categories(user_input: Sequence[str]) -> Tuple[Sequence[str], Sequence[str]]:
        """
        Categorizes user input in: For our AUR helper and for pacman

        :param user_input:  A sequence containing the user input as str
        :return:            Tuple containing two elements
                            First item: List containing the user input for our AUR helper
                            Second item: List containing the user input for pacman
        """
        for_us = []
        for_pacman = []
        user_input = list(set(user_input))

        found_in_aur_names = set([package.name for package in Package.get_packages_from_aur(user_input)])
        for _user_input in user_input:
            if _user_input in found_in_aur_names:
                for_us.append(_user_input)
            else:
                for_pacman.append(_user_input)

        return for_us, for_pacman

    @staticmethod
    def get_packages_from_aur(packages_names: Sequence[str]) -> List['Package']:
        """
        Generates and returns packages from the aur.
        see: https://wiki.archlinux.org/index.php/Arch_User_Repository

        :param packages_names:  The names of the packages to generate.
                                May not be empty.
        :return:                List containing the packages
        """
        aur_return = get_aur_info(packages_names)

        return_list = []

        for package_dict in aur_return:
            name = package_dict['Name']

            to_expand = {
                'name': name,
                'version': package_dict['Version'],
                'depends': package_dict.get('Depends', []),
                'conflicts': package_dict.get('Conflicts', []),
                'optdepends': package_dict.get('OptDepends', []),
                'provides': package_dict.get('Provides', []),
                'replaces': package_dict.get('Replaces', []),
                'pkgbase': package_dict['PackageBase'],
                'makedepends': package_dict.get('MakeDepends', []),
                'checkdepends': package_dict.get('CheckDepends', [])
            }

            if is_devel(name):
                to_expand['type_of'] = PossibleTypes.DEVEL_PACKAGE
            else:
                to_expand['type_of'] = PossibleTypes.AUR_PACKAGE

            return_list.append(Package(**to_expand))

        return return_list

    @staticmethod
    def get_packages_from_expac(expac_operation: str, packages_names: Sequence[str], packages_type: PossibleTypes) -> \
            List['Package']:
        """
        Generates and returns packages from an expac query.
        see: https://github.com/falconindy/expac

        :param expac_operation:     The expac operation. "-S" or "-Q".
        :param packages_names:      The names of the packages to generate.
                                    May also be empty, so that all packages are being returned.
        :param packages_type:       The type of the packages. PossibleTypes Enum value
        :return:                    List containing the packages
        """
        if "Q" in expac_operation:
            formatting = list("nvDHoPRewN")
        else:
            assert "S" in expac_operation
            formatting = list("nvDHoPRe")

        expac_return = expac(expac_operation, formatting, packages_names)
        return_list = []

        for line in expac_return:
            splitted_line = line.split("?!")
            to_expand = {
                'name': splitted_line[0],
                'version': splitted_line[1],
                'depends': splitted_line[2].split(),
                'conflicts': splitted_line[3].split(),
                'optdepends': splitted_line[4].split(),
                'provides': splitted_line[5].split(),
                'replaces': splitted_line[6].split()
            }

            if packages_type is PossibleTypes.AUR_PACKAGE or packages_type is PossibleTypes.DEVEL_PACKAGE:
                if is_devel(to_expand['name']):
                    type_to_set = PossibleTypes.DEVEL_PACKAGE
                else:
                    type_to_set = PossibleTypes.AUR_PACKAGE
            else:
                type_to_set = packages_type

            to_expand['type_of'] = type_to_set

            if splitted_line[7] == '(null)':
                to_expand['pkgbase'] = to_expand['name']
            else:
                to_expand['pkgbase'] = splitted_line[7]

            if len(splitted_line) >= 9:
                to_expand['install_reason'] = splitted_line[8]
                to_expand['required_by'] = splitted_line[9].split()

            if to_expand['name'] in to_expand['conflicts']:
                to_expand['conflicts'].remove(to_expand['name'])

            return_list.append(Package(**to_expand))

        return return_list

    def __init__(self, name: str, version: str, depends: Sequence[str] = None, conflicts: Sequence[str] = None,
                 required_by: Sequence[str] = None, optdepends: Sequence[str] = None, provides: Sequence[str] = None,
                 replaces: Sequence[str] = None, pkgbase: str = None, install_reason: str = None,
                 makedepends: Sequence[str] = None, checkdepends: Sequence[str] = None, type_of: PossibleTypes = None):
        self.name = name  # %n
        self.version = version  # %v
        self.depends = depends  # %D
        self.conflicts = conflicts  # %H
        self.required_by = required_by  # %N (only useful for installed packages)
        self.optdepends = optdepends  # %o
        self.provides = provides  # %P
        self.replaces = replaces  # %R
        self.pkgbase = pkgbase  # %e
        self.install_reason = install_reason  # %w (only with -Q)
        self.makedepends = makedepends  # aur only
        self.checkdepends = checkdepends  # aur only
        self.type_of = type_of  # PossibleTypes Enum value

    def __eq__(self, other):
        return isinstance(other, self.__class__) and self.name == other.name and self.version == other.version

    def __hash__(self):
        return hash((self.name, self.version))

    def __repr__(self):
        return "{}-{}".format(self.name, self.version)

    def relevant_deps(self) -> List[str]:
        """
        Fetches the relevant deps of this package.
        self.depends for not aur packages,
        otherwise also self.makedepends and self.checkdepends

        :return:
        """
        to_return = []

        if self.depends is not None:
            to_return.extend(self.depends)
        if self.makedepends is not None:
            to_return.extend(self.makedepends)
        if self.checkdepends is not None:
            to_return.extend(self.checkdepends)

        return to_return

    def solutions_for_dep_problem(self, solution: 'DepAlgoSolution', found_problems: Set['DepAlgoFoundProblems'],
                                  installed_system: 'System', upstream_system: 'System', only_unfulfilled_deps: bool,
                                  deps_to_deep_check: Set[str]) -> List['DepAlgoSolution']:
        """
        Heart of this AUR helper. Algorithm for dependency solving.
        Also checks for conflicts, dep-cycles and topologically sorts the solutions.

        :param solution:                The current solution
        :param found_problems:          A set containing found problems while searching for solutions
        :param installed_system:        The currently installed system
        :param upstream_system:         The system containing the known upstream packages
        :param only_unfulfilled_deps:   True (default) if one only wants to fetch unfulfilled deps packages, False otherwise
        :param deps_to_deep_check:      Set containing deps to check all possible dep providers of
        :return:                        The found solutions
        """
        if self in solution.packages_in_solution:
            return [deepcopy(solution)]

        # dep cycle
        # dirty... thanks to dep cycle between mesa and libglvnd
        if self in solution.visited_packages and not (self.type_of is PossibleTypes.REPO_PACKAGE):
            if solution.is_valid:
                index_of_self = solution.visited_packages.index(self)
                new_dep_cycle = DepAlgoCycle(solution.visited_packages[index_of_self:])
                new_dep_cycle.cycle_packages.append(self)
                found_problems.add(new_dep_cycle)
            return []
        elif self in solution.visited_packages:
            return [deepcopy(solution)]

        # conflict
        possible_conflict_packages = solution.visited_packages
        conflict_system = System(possible_conflict_packages).conflicting_with(self)
        if conflict_system:
            if solution.is_valid:
                min_index = min([solution.visited_packages.index(package) for package in conflict_system])
                way_to_conflict = solution.visited_packages[min_index:]
                way_to_conflict.append(self)
                new_conflict = DepAlgoConflict(set(conflict_system), way_to_conflict)
                new_conflict.conflicting_packages.add(self)
                found_problems.add(new_conflict)
            is_conflict = True
        else:
            is_conflict = False

        # copy solution and add self to visited packages, maybe flag as invalid
        solution = deepcopy(solution)
        solution.visited_packages.append(self)
        if is_conflict:
            solution.is_valid = False
        current_solutions = [solution]

        # AND - every dep has to be fulfilled
        for dep in self.relevant_deps():
            if only_unfulfilled_deps and installed_system.provided_by(dep):
                continue

            dep_providers = upstream_system.provided_by(dep)
            dep_providers_names = [package.name for package in dep_providers]
            dep_stripped_name = strip_versioning_from_name(dep)
            # dep not fulfillable, solutions not valid
            if not dep_providers:
                new_dep_not_fulfilled = DepAlgoNotProvided(dep, self)
                if new_dep_not_fulfilled not in found_problems:
                    found_problems.add(new_dep_not_fulfilled)

                for solution in current_solutions:
                    if dep not in solution.visited_names:
                        solution.is_valid = False
                        solution.visited_names.add(dep)

            # we only need relevant dep providers
            if dep_stripped_name in dep_providers_names and dep not in deps_to_deep_check:
                dep_providers = [package for package in dep_providers if package.name == dep_stripped_name]

            # OR - at least one of the dep providers needs to provide the dep
            finished_solutions = [solution for solution in current_solutions if dep in solution.visited_names]
            not_finished_solutions = [solution for solution in current_solutions if dep not in solution.visited_names]

            # check if dep provided by one of the packages already in a solution
            new_not_finished_solutions = []
            for solution in not_finished_solutions:
                solution.visited_names.add(dep)
                sol_system = System(solution.packages_in_solution)
                if sol_system.provided_by(dep):
                    finished_solutions.append(solution)
                else:
                    new_not_finished_solutions.append(solution)
            not_finished_solutions = new_not_finished_solutions

            # calc and append new solutions
            current_solutions = finished_solutions
            for solution in not_finished_solutions:
                for dep_provider in dep_providers:
                    current_solutions.extend(
                        dep_provider.solutions_for_dep_problem(solution, found_problems, installed_system,
                                                               upstream_system, only_unfulfilled_deps,
                                                               deps_to_deep_check))

        # we have valid solutions left, so the problems are not relevant
        if [solution for solution in current_solutions if solution.is_valid]:
            for problem in copy(found_problems):
                found_problems.remove(problem)

        # add self to packages in solution, those are always topologically sorted
        for solution in current_solutions:
            solution.packages_in_solution.append(self)

        # may contain invalid solutions !!!
        return current_solutions

    @staticmethod
    def dep_solving(packages: Sequence['Package'], installed_system: 'System', upstream_system: 'System',
                    only_unfulfilled_deps: bool) -> List[List['Package']]:
        """
        Solves deps for packages.

        :param packages:                The packages in a sequence
        :param installed_system:        The system containing the installed packages
        :param upstream_system:         The system containing the known upstream packages
        :param only_unfulfilled_deps:   True (default) if one only wants to fetch unfulfilled deps packages, False otherwise
        :return:                        A list containing the solutions.
                                        Every inner list contains the packages for the solution topologically sorted
        """

        current_solutions = [DepAlgoSolution([], [], set())]
        found_problems = set()
        deps_to_deep_check = set()

        while True:
            # calc solutions
            for package in packages:
                new_solutions = []
                for solution in current_solutions:
                    new_solutions.extend(
                        package.solutions_for_dep_problem(solution, found_problems, installed_system, upstream_system,
                                                          only_unfulfilled_deps, deps_to_deep_check))
                current_solutions = new_solutions

            # delete invalid solutions
            current_solutions = [solution for solution in current_solutions if solution.is_valid]

            # in case of at least one solution, we are done
            if current_solutions:
                break

            deps_to_deep_check_length = len(deps_to_deep_check)
            for problem in found_problems:
                problem_packages_names = set([package.name for package in problem.get_relevant_packages()])
                deps_to_deep_check |= problem_packages_names

            # if there are no new deps to deep check, we are done, too
            if len(deps_to_deep_check) == deps_to_deep_check_length:
                break

            found_problems = set()
            current_solutions = [DepAlgoSolution([], [], set())]

        # output for user
        if found_problems and not current_solutions:
            print("\nWhile searching for solutions the following errors occurred:\n{}\n".format(
                "\n\n".join([str(problem) for problem in found_problems])))

        return [solution.packages_in_solution for solution in current_solutions]

    def fetch_pkgbuild(self):
        """
        Fetches the current git aur repo changes for this package
        In cache_dir/package_base_name/.git/aurman will be copies of the last reviewed PKGBUILD and .install files
        In cache_dir/package_base_name/.git/aurman/.reviewed will be saved if the current PKGBUILD and .install files have been reviewed
        """

        package_dir = os.path.join(Package.cache_dir, self.pkgbase)
        git_aurman_dir = os.path.join(package_dir, ".git", "aurman")
        new_loaded = True

        # check if repo has ever been fetched
        if os.path.isdir(package_dir):
            if run("git fetch", shell=True, cwd=package_dir).returncode != 0:
                logging.error("git fetch of {} failed".format(self.name))
                raise ConnectionProblem("git fetch of {} failed".format(self.name))

            head = run("git rev-parse HEAD", shell=True, stdout=PIPE, universal_newlines=True,
                       cwd=package_dir).stdout.strip()
            u = run("git rev-parse @{u}", shell=True, stdout=PIPE, universal_newlines=True,
                    cwd=package_dir).stdout.strip()

            # if new sources available
            if head != u:
                if run("git reset --hard HEAD && git pull", shell=True, stdout=DEVNULL, stderr=DEVNULL,
                       cwd=package_dir).returncode != 0:
                    logging.error("sources of {} could not be fetched".format(self.name))
                    raise ConnectionProblem("sources of {} could not be fetched".format(self.name))
            else:
                new_loaded = False

        # repo has never been fetched
        else:
            # create package dir
            if run("install -dm700 '" + package_dir + "'", shell=True, stdout=DEVNULL, stderr=DEVNULL).returncode != 0:
                logging.error("Creating package dir of {} failed".format(self.name))
                raise InvalidInput("Creating package dir of {} failed".format(self.name))

            # clone repo
            if run("git clone https://aur.archlinux.org/" + self.pkgbase + ".git", shell=True,
                   cwd=Package.cache_dir).returncode != 0:
                logging.error("Cloning repo of {} failed".format(self.name))
                raise ConnectionProblem("Cloning repo of {} failed".format(self.name))

        # if aurman dir does not exist - create
        if not os.path.isdir(git_aurman_dir):
            if run("install -dm700 '" + git_aurman_dir + "'", shell=True, stdout=DEVNULL,
                   stderr=DEVNULL).returncode != 0:
                logging.error("Creating git_aurman_dir of {} failed".format(self.name))
                raise InvalidInput("Creating git_aurman_dir of {} failed".format(self.name))

        # files have not yet been reviewed
        if new_loaded:
            with open(os.path.join(git_aurman_dir, ".reviewed"), "w") as f:
                f.write("0")

    def show_pkgbuild(self):
        """
        Lets the user review and edit unreviewed PKGBUILD and install files of this package
        """

        package_dir = os.path.join(Package.cache_dir, self.pkgbase)
        git_aurman_dir = os.path.join(package_dir, ".git", "aurman")
        reviewed_file = os.path.join(git_aurman_dir, ".reviewed")

        # if package dir does not exist - abort
        if not os.path.isdir(package_dir):
            logging.error("Package dir of {} does not exist".format(self.name))
            raise InvalidInput("Package dir of {} does not exist".format(self.name))

        # if aurman dir does not exist - create
        if not os.path.isdir(git_aurman_dir):
            if run("install -dm700 '" + git_aurman_dir + "'", shell=True, stdout=DEVNULL,
                   stderr=DEVNULL).returncode != 0:
                logging.error("Creating git_aurman_dir of {} failed".format(self.name))
                raise InvalidInput("Creating git_aurman_dir of {} failed".format(self.name))

        # if reviewed file does not exist - create
        if not os.path.isfile(reviewed_file):
            with open(reviewed_file, "w") as f:
                f.write("0")

        # if files have been reviewed
        with open(reviewed_file, "r") as f:
            to_review = f.read().strip() == "0"

        if not to_review:
            return

        # relevant files are PKGBUILD + .install files
        relevant_files = ["PKGBUILD"]
        files_in_pack_dir = [f for f in os.listdir(package_dir) if os.path.isfile(os.path.join(package_dir, f))]
        for file in files_in_pack_dir:
            if file.endswith(".install"):
                relevant_files.append(file)

        # check if there are changes, if there are, ask the user if he wants to see them
        for file in relevant_files:
            if os.path.isfile(os.path.join(git_aurman_dir, file)):
                if run("git diff --no-index --quiet '" + "' '".join([os.path.join(git_aurman_dir, file), file]) + "'",
                       shell=True, cwd=package_dir).returncode == 1:
                    if ask_user("Do you want to view the changes of " + file + " of " + self.name + " ?", False):
                        run("git diff --no-index '" + "' '".join([os.path.join(git_aurman_dir, file), file]) + "'",
                            shell=True, cwd=package_dir)
                        changes_seen = True
                    else:
                        changes_seen = False
                else:
                    changes_seen = False
            else:
                if ask_user("Do you want to view the changes of " + file + " of " + self.name + " ?", False):
                    run("git diff --no-index '" + "' '".join([os.path.join("/dev", "null"), file]) + "'", shell=True,
                        cwd=package_dir)

                    changes_seen = True
                else:
                    changes_seen = False

            # if the user wanted to see changes, ask, if he wants to edit the file
            if changes_seen:
                if ask_user("Do you want to edit " + file + "?", False):
                    if run(Package.default_editor_path + " " + os.path.join(package_dir, file),
                           shell=True).returncode != 0:
                        logging.error("Editing {} failed".format(file))
                        raise InvalidInput("Editing {} failed".format(file))

        # if the user wants to use all files as they are now
        # copy all reviewed files to another folder for comparison of future changes
        if ask_user("Are you fine with using the files of {}?".format(self.name), True):
            with open(reviewed_file, "w") as f:
                f.write("1")

            for file in relevant_files:
                run("cp -f '" + "' '".join([file, os.path.join(git_aurman_dir, file)]) + "'", shell=True,
                    stdout=DEVNULL, stderr=DEVNULL, cwd=package_dir)

        else:
            logging.error("Files of {} are not okay".format(self.name))
            raise InvalidInput("Files of {} are not okay".format(self.name))

    def version_from_srcinfo(self) -> str:
        """
        Returns the version from the srcinfo
        :return:    The version read from the srcinfo
        """

        if self.pkgbase is None:
            logging.error("base package name of {} not known".format(self.name))
            raise InvalidInput("base package name of {} not known".format(self.name))

        package_dir = os.path.join(Package.cache_dir, self.pkgbase)
        if not os.path.isdir(package_dir):
            logging.error("package dir of {} does not exist".format(self.name))
            raise InvalidInput("package dir of {} does not exist".format(self.name))

        src_lines = makepkg("--printsrcinfo", True, package_dir)
        pkgver = None
        pkgrel = None
        epoch = None
        for line in src_lines:
            if "pkgver =" in line:
                pkgver = line.split("=")[1].strip()
            elif "pkgrel =" in line:
                pkgrel = line.split("=")[1].strip()
            elif "epoch =" in line:
                epoch = line.split("=")[1].strip()

        version = ""
        if epoch is not None:
            version += epoch + ":"
        if pkgver is not None:
            version += pkgver
        else:
            logging.info("version of {} must be there".format(self.name))
            raise InvalidInput("version of {} must be there".format(self.name))
        if pkgrel is not None:
            version += "-" + pkgrel

        return version

    def get_devel_version(self):
        """
        Fetches the current sources of this package.
        devel packages only!
        """

        package_dir = os.path.join(Package.cache_dir, self.pkgbase)
        makepkg("-odc --noprepare --skipinteg", False, package_dir)

        self.version = self.version_from_srcinfo()

    @staticmethod
    def get_build_dir(package_dir):
        """
        Gets the build directoy, if it is different from the package dir

        :param package_dir:     The package dir of the package
        :return:                The build dir in case there is one, the package dir otherwise
        """
        makepkg_conf = os.path.join("/etc", "makepkg.conf")
        if not os.path.isfile(makepkg_conf):
            logging.error("makepkg.conf not found")
            raise InvalidInput("makepkg.conf not found")

        with open(makepkg_conf, "r") as f:
            makepkg_conf_lines = f.read().strip().splitlines()

        for line in makepkg_conf_lines:
            line_stripped = line.strip()
            if line_stripped.startswith("PKGDEST="):
                return os.path.expandvars(os.path.expanduser(line_stripped.split("PKGDEST=")[1].strip()))
        else:
            return package_dir

    def get_package_file_to_install(self, build_dir: str, build_version: str) -> Union[str, None]:
        """
        Gets the .pkg. file of the package to install

        :param build_dir:       Build dir of the package
        :param build_version:   Build version to look for
        :return:                The name of the package file to install, None if there is none
        """
        files_in_build_dir = [f for f in os.listdir(build_dir) if os.path.isfile(os.path.join(build_dir, f))]
        for file in files_in_build_dir:
            if file.startswith(self.name + "-" + build_version + "-") and ".pkg." in \
                    file.split(self.name + "-" + build_version + "-")[1]:
                return file
        else:
            return None

    def build(self):
        """
        Build this package

        """
        # check if build needed
        build_version = self.version
        package_dir = os.path.join(Package.cache_dir, self.pkgbase)
        build_dir = Package.get_build_dir(package_dir)

        if self.get_package_file_to_install(build_dir, build_version) is None:
            makepkg("-cf --noconfirm", False, package_dir)

    def install(self, args_as_string: str):
        """
        Install this package

        :param args_as_string: Args for pacman
        """
        build_dir = Package.get_build_dir(os.path.join(Package.cache_dir, self.pkgbase))

        # get name of package install file
        build_version = self.version_from_srcinfo()
        package_install_file = self.get_package_file_to_install(build_dir, build_version)

        if package_install_file is None:
            logging.error("package file of {} not available".format(self.name))
            raise InvalidInput("package file of {} not available".format(self.name))

        # install
        pacman("{} {}".format(args_as_string, package_install_file), False, dir_to_execute=build_dir)


class System:

    @staticmethod
    def get_installed_packages() -> List['Package']:
        """
        Returns the installed packages on the system

        :return:    A list containing the installed packages
        """
        repo_packages_names = set(expac("-S", ('n',), ()))
        installed_packages_names = set(expac("-Q", ('n',), ()))
        installed_repo_packages_names = installed_packages_names & repo_packages_names
        unclassified_installed_names = installed_packages_names - installed_repo_packages_names

        return_list = []

        # installed repo packages
        if installed_repo_packages_names:
            return_list.extend(
                Package.get_packages_from_expac("-Q", list(installed_repo_packages_names), PossibleTypes.REPO_PACKAGE))

        # installed aur packages
        installed_aur_packages_names = set(
            [package.name for package in Package.get_packages_from_aur(list(unclassified_installed_names))])

        if installed_aur_packages_names:
            return_list.extend(
                Package.get_packages_from_expac("-Q", list(installed_aur_packages_names), PossibleTypes.AUR_PACKAGE))

        unclassified_installed_names -= installed_aur_packages_names

        # installed not repo not aur packages
        if unclassified_installed_names:
            return_list.extend(Package.get_packages_from_expac("-Q", list(unclassified_installed_names),
                                                               PossibleTypes.PACKAGE_NOT_REPO_NOT_AUR))

        return return_list

    @staticmethod
    def get_repo_packages() -> List['Package']:
        """
        Returns the current repo packages.

        :return:    A list containing the current repo packages
        """
        return Package.get_packages_from_expac("-S", (), PossibleTypes.REPO_PACKAGE)

    def __init__(self, packages: Sequence['Package']):
        self.all_packages_dict = {}  # names as keys and packages as values
        self.repo_packages_list = []  # list containing the repo packages
        self.aur_packages_list = []  # list containing the aur but not devel packages
        self.devel_packages_list = []  # list containing the aur devel packages
        self.not_repo_not_aur_packages_list = []  # list containing the packages that are neither repo nor aur packages

        # reverse dict for finding providings. names of providings as keys and providing packages as values in lists
        self.provides_dict = {}
        # same for conflicts
        self.conflicts_dict = {}

        self.append_packages(packages)

    def append_packages(self, packages: Sequence['Package']):
        """
        Appends packages to this system.

        :param packages:    The packages to append in a sequence
        """
        for package in packages:
            if package.name in self.all_packages_dict:
                logging.error("Package {} already known".format(package))
                raise InvalidInput("Package {} already known".format(package))

            self.all_packages_dict[package.name] = package

            if package.type_of is PossibleTypes.REPO_PACKAGE:
                self.repo_packages_list.append(package)
            elif package.type_of is PossibleTypes.AUR_PACKAGE:
                self.aur_packages_list.append(package)
            elif package.type_of is PossibleTypes.DEVEL_PACKAGE:
                self.devel_packages_list.append(package)
            else:
                assert package.type_of is PossibleTypes.PACKAGE_NOT_REPO_NOT_AUR
                self.not_repo_not_aur_packages_list.append(package)

        self.__append_to_x_dict(packages, 'provides')
        self.__append_to_x_dict(packages, 'conflicts')

    def __append_to_x_dict(self, packages: Sequence['Package'], dict_name: str):
        dict_to_append_to = getattr(self, "{}_dict".format(dict_name))

        for package in packages:
            relevant_package_values = getattr(package, dict_name)

            for relevant_value in relevant_package_values:
                value_name = strip_versioning_from_name(relevant_value)
                if value_name in dict_to_append_to:
                    dict_to_append_to[value_name].append(package)
                else:
                    dict_to_append_to[value_name] = [package]

    def provided_by(self, dep: str) -> List['Package']:
        """
        Providers for the dep

        :param dep:     The dep to be provided
        :return:        List containing the providing packages
        """

        dep_name, dep_cmp, dep_version = split_name_with_versioning(dep)
        return_list = []

        if dep_name in self.all_packages_dict:
            package = self.all_packages_dict[dep_name]
            if dep_cmp == "":
                return_list.append(package)
            elif version_comparison(package.version, dep_cmp, dep_version):
                return_list.append(package)

        if dep_name in self.provides_dict:
            possible_packages = self.provides_dict[dep_name]
            for package in possible_packages:

                if package in return_list:
                    continue

                for provide in package.provides:
                    provide_name, provide_cmp, provide_version = split_name_with_versioning(provide)

                    if provide_name != dep_name:
                        continue

                    if dep_cmp == "":
                        return_list.append(package)
                    elif (provide_cmp == "=" or provide_cmp == "==") and version_comparison(provide_version, dep_cmp,
                                                                                            dep_version):
                        return_list.append(package)
                    elif (provide_cmp == "") and version_comparison(package.version, dep_cmp, dep_version):
                        return_list.append(package)

        return return_list

    def conflicting_with(self, package: 'Package') -> List['Package']:
        """
        Returns the packages conflicting with "package"

        :param package:     The package to check for conflicts with
        :return:            List containing the conflicting packages
        """
        name = package.name
        version = package.version

        return_list = []

        if name in self.all_packages_dict:
            possible_conflict_package = self.all_packages_dict[name]
            if version != possible_conflict_package.version:
                return_list.append(possible_conflict_package)

        for conflict in package.conflicts:
            conflict_name, conflict_cmp, conflict_version = split_name_with_versioning(conflict)

            if conflict_name not in self.all_packages_dict:
                continue

            possible_conflict_package = self.all_packages_dict[conflict_name]

            if possible_conflict_package in return_list:
                continue

            if conflict_cmp == "":
                return_list.append(possible_conflict_package)
            elif version_comparison(possible_conflict_package.version, conflict_cmp, conflict_version):
                return_list.append(possible_conflict_package)

        if name in self.conflicts_dict:
            possible_conflict_packages = self.conflicts_dict[name]
            for possible_conflict_package in possible_conflict_packages:

                if possible_conflict_package in return_list:
                    continue

                for conflict in possible_conflict_package.conflicts:
                    conflict_name, conflict_cmp, conflict_version = split_name_with_versioning(conflict)

                    if conflict_name != name:
                        continue

                    if conflict_cmp == "":
                        return_list.append(possible_conflict_package)
                    elif version_comparison(version, conflict_cmp, conflict_version):
                        return_list.append(possible_conflict_package)

        return return_list

    def append_packages_by_name(self, packages_names: Sequence[str]):
        """
        Appends packages to this system by names.

        :param packages_names:          The names of the packages
        """

        packages_names = set([strip_versioning_from_name(name) for name in packages_names])
        packages_names_to_fetch = [name for name in packages_names if name not in self.all_packages_dict]

        while packages_names_to_fetch:
            fetched_packages = Package.get_packages_from_aur(packages_names_to_fetch)
            self.append_packages(fetched_packages)

            deps_of_the_fetched_packages = []
            for package in fetched_packages:
                deps_of_the_fetched_packages.extend(package.relevant_deps())

            relevant_deps = list(set([strip_versioning_from_name(dep) for dep in deps_of_the_fetched_packages]))

            packages_names_to_fetch = [dep for dep in relevant_deps if dep not in self.all_packages_dict]

    def are_all_deps_fulfilled(self, package: 'Package') -> bool:
        """
        if all deps of the package are fulfilled on the system
        :param package:     the package to check the deps of
        :return:            True if the deps are fulfilled, False otherwise
        """

        for dep in package.relevant_deps():
            if not self.provided_by(dep):
                return False
        else:
            return True

    def hypothetical_append_packages_to_system(self, packages: Sequence['Package']) -> 'System':
        """
        hypothetically appends packages to this system (only makes sense for the installed system)
        and removes all conflicting packages and packages whose deps are not fulfilled anymore.

        :param packages:    the packages to append
        :return:            the new system
        """

        new_system = deepcopy(self)

        deleted_packages = []
        for package in packages:
            if package.name in new_system.all_packages_dict:
                deleted_packages.append(new_system.all_packages_dict[package.name])
                del new_system.all_packages_dict[package.name]
        new_system = System(list(new_system.all_packages_dict.values()))

        to_delete_packages = []
        for package in packages:
            to_delete_packages.extend(new_system.conflicting_with(package))
        to_delete_packages = list(set(to_delete_packages))
        new_system.append_packages(packages)

        while to_delete_packages or deleted_packages:
            for to_delete_package in to_delete_packages:
                deleted_packages.append(to_delete_package)
                del new_system.all_packages_dict[to_delete_package.name]
            new_system = System(list(new_system.all_packages_dict.values()))

            to_delete_packages = []
            was_required_by_packages = []
            for deleted_package in deleted_packages:
                if deleted_package.required_by is not None:
                    was_required_by_packages.extend(
                        [new_system.all_packages_dict[required_by] for required_by in deleted_package.required_by if
                         required_by in new_system.all_packages_dict])
            deleted_packages = []

            for was_required_by_package in was_required_by_packages:
                if not new_system.are_all_deps_fulfilled(was_required_by_package):
                    if was_required_by_package not in to_delete_packages:
                        to_delete_packages.append(was_required_by_package)

        while True:
            to_delete_packages = []
            for package in packages:
                if package.name in new_system.all_packages_dict:
                    if not new_system.are_all_deps_fulfilled(package):
                        to_delete_packages.append(package)

            if not to_delete_packages:
                return new_system

            for to_delete_package in to_delete_packages:
                del new_system.all_packages_dict[to_delete_package.name]
            new_system = System(list(new_system.all_packages_dict.values()))

    def differences_between_systems(self, other_systems: Sequence['System']) -> Tuple[
        Tuple[Set['Package'], Set['Package']], List[Tuple[Set['Package'], Set['Package']]]]:
        """
        Evaluates differences between this system and other systems.

        :param other_systems:   The other systems.
        :return:                A tuple containing two items:

                                First item:
                                    Tuple containing two items:

                                    First item:
                                        installed packages in respect to this system,
                                        which are in all other systems
                                    Second item:
                                        uninstalled packages in respect to this system,
                                        which are in all other systems

                                Second item:
                                    List containing tuples with two items each:

                                    For the i-th tuple (all in all as many tuples as other systems):
                                        First item:
                                            installed packages in respect to this system,
                                            which are in the i-th system but not in all systems
                                        Second item:
                                            uninstalled packages in respect to this system,
                                            which are in the i-th system but not in all systems
        """

        differences_tuples = []
        own_packages = set(self.all_packages_dict.values())

        for other_system in other_systems:
            current_difference_tuple = (set(), set())
            differences_tuples.append(current_difference_tuple)
            other_packages = set(other_system.all_packages_dict.values())
            difference = own_packages ^ other_packages

            for differ in difference:
                if differ not in own_packages:
                    current_difference_tuple[0].add(differ)
                else:
                    current_difference_tuple[1].add(differ)

        first_return_tuple = (set.intersection(*[difference_tuple[0] for difference_tuple in differences_tuples]),
                              set.intersection(*[difference_tuple[1] for difference_tuple in differences_tuples]))

        return_list = []

        for difference_tuple in differences_tuples:
            current_tuple = (set(), set())
            return_list.append(current_tuple)

            for installed_package in difference_tuple[0]:
                if installed_package not in first_return_tuple[0]:
                    current_tuple[0].add(installed_package)

            for uninstalled_package in difference_tuple[1]:
                if uninstalled_package not in first_return_tuple[1]:
                    current_tuple[1].add(uninstalled_package)

        return first_return_tuple, return_list

    def validate_and_choose_solution(self, solutions: List[List['Package']],
                                     needed_packages: Sequence['Package']) -> List['Package']:
        """
        Validates solutions and lets the user choose a solution

        :param solutions:           The solutions
        :param needed_packages:     Packages which need to be in the solutions
        :return:                    A chosen and valid solution
        """

        # needed strings
        different_solutions_found = "\nWe found {} different valid solutions.\nYou will be shown the differences between the solutions.\nChoose one of them by entering the corresponding number.\n"
        solution_print = "\nNumber {}:\nGetting installed: {}\nGetting removed: {}\n"
        choice_not_valid = color_string((Colors.LIGHT_RED, "That was not a valid choice!"))

        # calculating new systems and finding valid systems
        new_systems = [self.hypothetical_append_packages_to_system(solution) for solution in solutions]
        valid_systems = []
        valid_solutions_indices = []
        for i, new_system in enumerate(new_systems):
            for package in needed_packages:
                if package.name not in new_system.all_packages_dict:
                    break
            else:
                valid_systems.append(new_system)
                valid_solutions_indices.append(i)

        # no valid solutions
        if not valid_systems:
            logging.error("No valid solutions found")
            raise InvalidInput("No valid solutions found")

        # only one valid solution - just return
        if len(valid_systems) == 1:
            return solutions[valid_solutions_indices[0]]

        # calculate the differences between the resulting systems for the valid solutions
        systems_differences = self.differences_between_systems(valid_systems)

        # if the solutions are different but the resulting systems are not
        single_differences_count = sum(
            [len(diff_tuple[0]) + len(diff_tuple[1]) for diff_tuple in systems_differences[1]])
        if single_differences_count == 0:
            return solutions[valid_solutions_indices[0]]

        # delete duplicate resulting systems
        new_valid_systems = []
        new_valid_solutions_indices = []
        diff_set = set()
        for i, valid_system in enumerate(valid_systems):
            cont_set = frozenset(set.union(systems_differences[1][i][0], systems_differences[1][i][1]))
            if cont_set not in diff_set:
                new_valid_systems.append(valid_system)
                diff_set.add(cont_set)
                new_valid_solutions_indices.append(valid_solutions_indices[i])
        valid_systems = new_valid_systems
        valid_solutions_indices = new_valid_solutions_indices
        systems_differences = self.differences_between_systems(valid_systems)

        # print for the user
        print(color_string((Colors.DEFAULT, different_solutions_found.format(len(valid_systems)))))

        while True:
            # print solutions
            for i in range(0, len(valid_systems)):
                installed_names = [package.name for package in systems_differences[1][i][0]]
                removed_names = [package.name for package in systems_differences[1][i][1]]
                installed_names.sort()
                removed_names.sort()

                print(solution_print.format(i + 1,
                                            ", ".join([color_string((Colors.GREEN, name)) for name in installed_names]),
                                            ", ".join([color_string((Colors.RED, name)) for name in removed_names])))

            try:
                user_input = int(input(color_string((Colors.DEFAULT, "Enter the number: "))))
                if 1 <= user_input <= len(valid_systems):
                    return solutions[valid_solutions_indices[user_input - 1]]
            except ValueError:
                print(choice_not_valid)
            else:
                print(choice_not_valid)

    def show_solution_differences_to_user(self, solution: List['Package']):
        """
        Shows the chosen solution to the user with package upgrades etc.

        :param solution:    The chosen solution
        """

        # needed strings
        package_to_install = "\nThe following {} package(s) are getting installed:\n"
        packages_to_uninstall = "\nThe following {} package(s) are getting removed:\n"
        packages_to_upgrade = "\nThe following {} package(s) are getting updated:\n"
        packages_to_reinstall = "\nThe following {} packages(s) are just getting reinstalled:\n"
        user_question = "\nDo you want to continue?"

        new_system = self.hypothetical_append_packages_to_system(solution)
        differences_to_this_system_tuple = self.differences_between_systems((new_system,))[0]

        to_install_names = set([package.name for package in differences_to_this_system_tuple[0]])
        to_uninstall_names = set([package.name for package in differences_to_this_system_tuple[1]])
        to_upgrade_names = to_install_names & to_uninstall_names
        to_install_names -= to_upgrade_names
        to_uninstall_names -= to_upgrade_names
        just_reinstall_names = set([package.name for package in solution]) - set.union(
            *[to_upgrade_names, to_install_names, to_uninstall_names])

        print(color_string((Colors.DEFAULT, package_to_install.format(len(to_install_names)))))
        print(", ".join(
            [color_string((Colors.GREEN, str(new_system.all_packages_dict[package_name]))) for package_name in
             to_install_names]))

        print(color_string((Colors.DEFAULT, packages_to_uninstall.format(len(to_uninstall_names)))))
        print(", ".join([color_string((Colors.RED, str(self.all_packages_dict[package_name]))) for package_name in
                         to_uninstall_names]))

        print(color_string((Colors.DEFAULT, packages_to_upgrade.format(len(to_upgrade_names)))))
        print(''.join(["{} -> {}\n".format(color_string((Colors.RED, str(self.all_packages_dict[package_name]))),
                                           color_string(
                                               (Colors.GREEN, str(new_system.all_packages_dict[package_name])))) for
                       package_name in to_upgrade_names]))

        print(color_string((Colors.DEFAULT, packages_to_reinstall.format(len(just_reinstall_names)))))
        print(", ".join(
            [color_string((Colors.LIGHT_MAGENTA, str(self.all_packages_dict[package_name]))) for package_name in
             just_reinstall_names]))

        if not ask_user(color_string((Colors.DEFAULT, user_question)), True):
            raise InvalidInput()
