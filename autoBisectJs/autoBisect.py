#!/usr/bin/env python
#
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

import os
import platform
import shutil
import subprocess
import sys
import re
import tempfile
from optparse import OptionParser
from types import *

path0 = os.path.dirname(__file__)
path1 = os.path.abspath(os.path.join(path0, os.pardir, 'interestingness'))
sys.path.append(path1)
import ximport

path2 = os.path.abspath(os.path.join(path0, os.pardir, 'util'))
sys.path.append(path2)
from subprocesses import captureStdout, dateStr, isVM, normExpUserPath, vdump

path3 = os.path.abspath(os.path.join(path0, os.pardir, 'js'))
sys.path.append(path3)
from compileShell import autoconfRun, cfgJsBin, compileCopy, shellName

verbose = False

# autoBisect uses temporary directory python APIs. On WinXP, these are located at
# c:\docume~1\mozilla\locals~1\temp\ and the ~ in the shortened folders break pymake.
# This can be fixed by moving compilations to autobisect-cache, but we lose the benefit of
# compiling in a temporary directory. Not worth it, for an OS that is on its way out.
#assert platform.uname()[2] != 'XP'
# Disable autoBisect when running in a VM, even Linux. This has the possibility of interacting with
# the repositories in the trees directory as they can update to a different changeset within the VM.
# It should work when running manually though.
assert isVM()[1] == False

COMPILATION_FAILED_LABEL = 'skip'

shellCacheDirStart = os.path.join('c:', os.sep) if isVM() == ('Windows', True) \
    else os.path.join('~', 'Desktop')
shellCacheDir = normExpUserPath(os.path.join(shellCacheDirStart, 'autobisect-cache'))
if not os.path.exists(shellCacheDir):
    os.mkdir(shellCacheDir)

def main():
    print dateStr()
    global hgPrefix
    global shellCacheDir

    # Parse options and parameters from the command-line.
    options = parseOpts()
    (compileType, sourceDir, stdoutOutput, resetBool, startRepo, endRepo, paranoidBool, \
     archNum, flagsRequired, watchExitCode, valgrindSupport, testAndLabel) = options

    sourceDir = os.path.expanduser(sourceDir)
    hgPrefix = ['hg', '-R', sourceDir]
    if startRepo is None:
        startRepo = earliestKnownWorkingRev(flagsRequired, archNum, valgrindSupport)

    # Resolve names such as "tip", "default", or "52707" to stable hg hash ids
    # such as "9f2641871ce8".
    realStartRepo = startRepo = hgId(startRepo)
    realEndRepo = endRepo = hgId(endRepo)

    vdump("Bisecting in the range " + startRepo + ":" + endRepo)

    # Refresh source directory (overwrite all local changes) to default tip if required.
    if resetBool:
        subprocess.call(hgPrefix + ['up', '-C', 'default'])
        # XXX should also "hg purge" here, but "purge" is an extension.

    labels = {}

    # Reset `hg bisect`
    captureStdout(hgPrefix + ['bisect', '-r'])

    # Skip some busted revisions.
    # It might make sense to avoid (or note) these in checkBlameParents.
    # 1. descendants(eae8350841be) - descendants(f3e58c264932) [partial]
    # Note: The following instructions are untested.
    # To add to the list of descendant revsets:
    # - Temporarily set COMPILATION_FAILED_LABEL in autoBisect.py to 'bad' instead of 'skip'
    # - Then take one of the revs that fails, say fd756976e52c
    # - 404.js does not need to exist, but assuming tip / default works,
    # - (1) will tell you when the brokenness started
    # - (1) autoBisect.py -p -a32 -s fd756976e52c 404.js
    # - (2) will tell you when the brokenness ended
    # - (2) autoBisect.py -p -a32 -e fd756976e52c 404.js
    # Alternative: (descendants(last good changeset)-descendants(first working changeset))
    captureStdout(hgPrefix + ['bisect', '--skip', 'eae8350841be'])
    captureStdout(hgPrefix + ['bisect', '--skip', 'e5958cd4a135'])
    captureStdout(hgPrefix + ['bisect', '--skip', 'd575f16c7f55']) # an ill-timed merge into the jaegermonkey repository!
    captureStdout(hgPrefix + ['bisect', '--skip', '0d5d2ceb9436'])
    captureStdout(hgPrefix + ['bisect', '--skip', 'e6496cd735a6'])
    captureStdout(hgPrefix + ['bisect', '--skip', '(descendants(8de0a7fef2c0)-descendants(d43e89d8a20b))'], ignoreStderr=True, ignoreExitCode=True) # early jaeger
    captureStdout(hgPrefix + ['bisect', '--skip', '(descendants(a6c636740fb9)-descendants(ca11457ed5fe))'], ignoreStderr=True, ignoreExitCode=True) # a large backout
    captureStdout(hgPrefix + ['bisect', '--skip', '(descendants(c12c8651c10d)-descendants(723d44ef6eed))'], ignoreStderr=True, ignoreExitCode=True) # m-c to tm merge that broke compilation

    # Specify `hg bisect` ranges.
    if paranoidBool:
        currRev = startRepo
    else:
        labels[startRepo] = ('good', 'assumed start rev is good')
        labels[endRepo] = ('bad', 'assumed end rev is bad')
        captureStdout(hgPrefix + ['bisect', '-U', '-g', startRepo])
        currRev = extractChangesetFromMessage(firstLine(captureStdout(hgPrefix + ['bisect', '-U', '-b', endRepo])[0]))

    testRev = makeTestRev(shellCacheDir, sourceDir, archNum, compileType, valgrindSupport, testAndLabel)

    iterNum = 1
    if paranoidBool:
        iterNum -= 2
    while currRev is not None:
        label = testRev(currRev)
        labels[currRev] = label
        print label[0] + " (" + label[1] + ") ",

        if iterNum <= 0:
            print "Paranoid test finished..."
        else:
            print "Bisecting for the n-th round where n is", iterNum, "and 2^n is", str(2**iterNum), "..."
        (currRev, blamedGoodOrBad, blamedRev, startRepo, endRepo) = bisectLabel(label[0], currRev, startRepo, endRepo, paranoidBool)

        if paranoidBool:
            paranoidBool = False
            assert currRev is None
            currRev = endRepo

        iterNum += 1

    if blamedRev is not None:
        checkBlameParents(blamedRev, blamedGoodOrBad, labels, testRev, realStartRepo, realEndRepo)

    vdump("Resetting bisect")
    subprocess.call(hgPrefix + ['bisect', '-U', '-r'])

    vdump("Resetting working directory")
    captureStdout(hgPrefix + ['up', '-r', 'default'], ignoreStderr=True)

    print dateStr()

def findCommonAncestor(a, b):
    # Requires hg 1.6 for the revset feature
    return captureStdout(hgPrefix + ["log", "--template={node|short}", "-r", "ancestor("+a+","+b+")"])[0]

def isAncestor(a, b):
    return findCommonAncestor(a, b) == a

def checkBlameParents(blamedRev, blamedGoodOrBad, labels, testRev, startRepo, endRepo):
    """Ensure we actually tested the parents of the blamed revision."""
    parents = captureStdout(hgPrefix + ["parent", '--template={node|short},', "-r", blamedRev])[0].split(",")[:-1]
    bisectLied = False
    for p in parents:
        testedLastMinute = False
        if labels.get(p) is None:
            print ""
            print "Oops! We didn't test rev %s, a parent of the blamed revision! Let's do that now." % p
            if not isAncestor(startRepo, p) and not isAncestor(endRepo, p):
                print "We did not test rev %s because it is not a descendant of either %s or %s." % (p, startRepo, endRepo)
            label = testRev(p)
            labels[p] = label
            print label[0] + " (" + label[1] + ") "
            testedLastMinute = True
        if labels[p][0] == "skip":
            print "Parent rev %s was marked as 'skip', so the regression window includes it."
        elif labels[p][0] == blamedGoodOrBad:
            print "Bisect lied to us! Parent rev %s was also %s!" % (p, blamedGoodOrBad)
            bisectLied = True
        else:
            if verbose or testedLastMinute:
                print "As expected, the parent's label is the opposite of the blamed rev's label."
            assert labels[p][0] == {'good': 'bad', 'bad': 'good'}[blamedGoodOrBad]
    if len(parents) == 2 and bisectLied:
        print ""
        print "Perhaps we should expand the search to include the common ancestor of the blamed changeset's parents."
        ca = findCommonAncestor(parents[0], parents[1])
        print "The common ancestor of %s and %s is %s." % (parents[0], parents[1], ca)
        label = testRev(ca)
        print label[0] + " (" + label[1] + ") "
        print "Try setting -s to %s, and -e to %s, and re-run autoBisect." % (ca, parents[0])

def makeTestRev(shellCacheDir, sourceDir, archNum, compileType, valgrindSupport, testAndLabel):
    def testRev(rev):
        cachedShell = os.path.join(shellCacheDir, shellName(archNum, compileType, rev, valgrindSupport))
        cachedNoShell = cachedShell + ".busted"

        print "Rev " + rev + ":",
        if os.path.exists(cachedShell):
            jsShellName = cachedShell
            print "Found cached shell...   ",
        elif os.path.exists(cachedNoShell):
            return (COMPILATION_FAILED_LABEL, 'compilation failed (cached)')
        else:
            print "Updating...",
            captureStdout(hgPrefix + ['update', '-r', rev], ignoreStderr=True)
            try:
                print "Compiling...",
                jsShellName = makeShell(shellCacheDir, sourceDir,
                                        archNum, compileType, valgrindSupport,
                                        rev)
            except Exception, e:
                open(cachedNoShell, 'w').close()
                return (COMPILATION_FAILED_LABEL, 'compilation failed (' + str(e) + ')')

        print "Testing...",
        return testAndLabel(jsShellName, rev)
    return testRev

def internalTestAndLabel(filename, flagsRequired, valgrindSupport, stdoutOutput, watchExitCode):
    def inner(jsShellName, rev):
        (stdoutStderr, exitCode) = testBinary(jsShellName, filename, flagsRequired, valgrindSupport)

        if (stdoutStderr.find(stdoutOutput) != -1) and (stdoutOutput != ''):
            return ('bad', 'Specified-bad output')
        elif watchExitCode != None and exitCode == watchExitCode:
            return ('bad', 'Specified-bad exit code ' + str(exitCode))
        elif watchExitCode == None and 129 <= exitCode <= 159:
            return ('bad', 'High exit code ' + str(exitCode))
        elif exitCode < 0:
            # On Unix-based systems, the exit code for signals is negative,
            # so we check if 128 + abs(exitCode) meets our specified signal
            # exit code.
            if (watchExitCode != None and 128 - exitCode == watchExitCode):
                return ('bad', 'Specified-bad exit code ' + str(exitCode) + ' (after converting to signal)')
            else:
                return ('bad', 'Negative exit code ' + str(exitCode))
        elif exitCode == 0:
            return ('good', 'Exit code 0')
        elif exitCode == 2 and (stdoutStderr.find('usage: js [') != -1) and (stdoutOutput != ''):
            return ('good', 'Exit code 2 - js shell quits because it does not support a given CLI parameter')
        elif 3 <= exitCode <= 6:
            return ('good', 'Acceptable exit code ' + str(exitCode))
        elif watchExitCode != None:
            return ('good', 'Unknown exit code ' + str(exitCode) + ', but not the specified one')
        else:
            return ('bad', 'Unknown exit code ' + str(exitCode))
    return inner

def externalTestAndLabel(filename, flagsRequired, interestingness):
    conditionScript = ximport.importRelativeOrAbsolute(interestingness[0])
    conditionArgPrefix = interestingness[1:]

    tempPrefix = os.path.join(tempfile.mkdtemp(), "x")

    def inner(jsShellName, rev):
        conditionArgs = conditionArgPrefix + [jsShellName] + flagsRequired + [filename]
        if hasattr(conditionScript, "init"):
            # Since we're changing the js shell name, call init() again!
            conditionScript.init(conditionArgs)
        if conditionScript.interesting(conditionArgs, tempPrefix + rev):
            return ('bad', 'interesting')
        else:
            return ('good', 'not interesting')
    return inner

def parseOpts():
    usage = 'Usage: %prog [options] filename'
    parser = OptionParser(usage)
    # See http://docs.python.org/library/optparse.html#optparse.OptionParser.disable_interspersed_args
    parser.disable_interspersed_args()

    if isVM() == ('Windows', True):
        mcRepoDirStart = os.path.join('z:', os.sep)
    elif isVM() == ('Linux', True):
        mcRepoDirStart = os.path.join('/', 'mnt', 'hgfs')
    else:
        mcRepoDirStart = '~'
    mcRepoDir = normExpUserPath(os.path.join(mcRepoDirStart, 'trees', 'mozilla-central'))
    # Define the repository (working directory) in which to bisect.
    parser.add_option('-d', '--dir',
                      dest='dir',
                      default=mcRepoDir,
                      help='Source code directory. Defaults to "' + mcRepoDir + '"')
    parser.add_option('-r', '--resetToTipFirstBool',
                      dest='resetBool',
                      action='store_true',
                      default=False,
                      help='First reset to default tip overwriting all local changes. ' + \
                           'Equivalent to first executing `hg update -C default`. ' + \
                           'Defaults to "False"')

    # Define the revisions between which to bisect.
    # If you want to find out when a problem *went away*, give -s the later revision and -e an earlier revision,
    # or use -p (in which case the order doesn't matter).
    parser.add_option('-s', '--start',
                      dest='startRepo',
                      help='Initial good revision (usually the earliest). Defaults to the earliest revision known to work at all.')
    parser.add_option('-e', '--end',
                      dest='endRepo',
                      default='default',
                      help='Initial bad revision (usually the latest). Defaults to "default"')
    parser.add_option('-p', '--paranoid',
                      dest='paranoidBool',
                      action='store_true',
                      default=False,
                      help='Test the -s and -e revisions (rather than automatically treating them as -g and -b).')

    # Define the type of build to test.
    parser.add_option('-a', '--architecture',
                      dest='archi',
                      type='choice',
                      choices=['32', '64'],
                      help='Test architecture. Only accepts "32" or "64"')
    parser.add_option('-c', '--compileType',
                      dest='compileType',
                      type='choice',
                      choices=['dbg', 'opt'],
                      default='dbg',
                      help='js shell compile type. Defaults to "dbg"')

    # Define specific type of failure to look for (optional).
    parser.add_option('-o', '--output',
                      dest='output',
                      default='',
                      help='Stdout or stderr output to be observed. Defaults to "". ' + \
                           'For assertions, set to "ssertion fail"')
    parser.add_option('-w', '--watchExitCode',
                      dest='watchExitCode',
                      type='int',
                      default=None,
                      help='Look out for a specific exit code. Only this exit code will be considered bad.')
    parser.add_option('-i', '--interestingness',
                      dest='interestingnessBool',
                      default=False,
                      action="store_true",
                      help="Interpret the final arguments as an interestingness test")

    # Define parameters to be passed to the binary.
    parser.add_option('--flags',
                      dest='flagsRequired',
                      default=[],
                      help='Define the flags to reproduce the bug, e.g. "-m,-j". Defaults to ""')

    # Enable valgrind support.
    parser.add_option('-v', '--valgrind',
                      dest='valgSupport',
                      action='store_true',
                      default=False,
                      help='Enable valgrind support. Defaults to "False"')

    (options, args) = parser.parse_args()

    flagsReq = options.flagsRequired

    if type(flagsReq) is StringType:
        flagsReq = ',' + flagsReq
        flagsReq = flagsReq.split(',')

    assert type(flagsReq) is ListType, "--flags is not a list: %s" % flagsReq

    flagsReq = filter(None, flagsReq)  # Remove empty list entries
    assert '' not in flagsReq

    if len(args) < 1:
        parser.error('Not enough arguments')
    filename = args[0]

    if options.interestingnessBool:
        if len(args) < 2:
            parser.error('Not enough arguments.')
        testAndLabel = externalTestAndLabel(filename, flagsReq, args[1:])
    else:
        if len(args) >= 2:
            parser.error('Too many arguments.')
        testAndLabel = internalTestAndLabel(filename, flagsReq, options.valgSupport, options.output, options.watchExitCode)


    return options.compileType, options.dir, options.output, \
            options.resetBool, options.startRepo, options.endRepo, options.paranoidBool, options.archi, \
            flagsReq, options.watchExitCode, options.valgSupport, testAndLabel

def hgId(rev):
    return captureStdout(hgPrefix + ["id", "-i", "-r", rev])[0]

def earliestKnownWorkingRev(flagsRequired, archNum, valgrindSupport):
    """Returns the oldest version of the shell that can run jsfunfuzz."""
    # Unfortunately, there are also interspersed runs of brokenness, such as:
    # * 0c8d4f846be8::bfb330182145 (~28226::28450).
    # * 1558cef8a8a0::e81fa1f189dc (~51206::51210 plus merges) (see bug 590519) ('rdtsc' was not declared in this scope)
    # * dd0b2f4d5299::???????????? (perhaps 64-bit only)
    # To make matters worse, merges between mozilla-central and tracemonkey might have happened during
    # the brokenness, resulting in a large number of additional broken changesets
    # in "descendants(x) - descendants(y)".
    # We don't deal with those at all, and --skip does not get out of such messes quickly.

    if platform.system() == 'Darwin':
        (isSL, isLion) = macType()

    profilejitBool = True if '-p' in flagsRequired else False
    methodjitBool = True if '-m' in flagsRequired else False
    methodjitAllBool = True if '-a' in flagsRequired else False
    typeInferBool = True if '-n' in flagsRequired else False
    debugModeBool = True if '-d' in flagsRequired else False
    ionBool = True if '--ion' in flagsRequired else False

    # These should be in descending order, or bisection will break at earlier changesets.
    if ionBool:
        return '43b55878da46' # IonMonkey has not yet landed on m-c, approximate first stable rev w/ --ion -n.
    elif typeInferBool:
        return '228e319574f9' # 74704 on m-c, first rev that has the -n option
    elif isMac and isLion:
        return 'd796fb18f555' # 64560 on m-c, first rev that can compile on Lion
    elif methodjitAllBool:
        # This supercedes methodjitBool, -a only works with -m
        return 'f569d49576bb' # 62574 on m-c, first rev that has the -a option
    elif profilejitBool:
        return '339457364540' # 56551 on m-c, first rev that has the -p option
    elif debugModeBool:
        # To bisect farther back, use setDebug(true). See bug 656381 comment 0.
        return 'ea0669bacf12' # 54578 on m-c, first rev that has the -d option
    elif methodjitBool and platform.system() == 'Windows':
        return '9f2641871ce8' # 53544 on m-c, first rev that can run with pymake and -m
    elif methodjitBool:
        return '547af2626088' # 53105 on m-c, first rev that can run jsfunfuzz-n.js with -m
    elif platform.system() == 'Windows':
        return 'ea59b927d99f' # 46436 on m-c, first rev that can run pymake on Windows with most recent set of instructions
    elif isMac and isSL and archNum == "64":
        return "1a44373ccaf6" # 32315 on m-c, config.guess change for snow leopard
    elif (os.uname()[0] == 'Linux') or (isMac and isSL and archNum == "32"):
        return "db4d22859940" # 24546 on m-c, imacros compilation change
    elif valgrindSupport:
        assert False  # This should no longer be reached since Ubuntu 11.04 has difficulties compiling earlier changesets.
        return "582a62c8f910" # 21512 on m-c, fixed a regexp valgrind warning that is triggered by an empty jsfunfuzz testcase
    else:
        assert False  # This should no longer be reached since Ubuntu 11.04 has difficulties compiling earlier changesets.
        return "8c52a9486c8f" # 21062 on m-c, switch from Makefile.ref to autoconf

def extractChangesetFromMessage(str):
    # For example, a bisect message like "Testing changeset 41831:4f4c01fb42c3 (2 changesets remaining, ~1 tests)"
    r = re.compile(r"(^|.* )(\d+):(\w{12}).*")
    m = r.match(str)
    if m:
        return m.group(3)

assert extractChangesetFromMessage("x 12345:abababababab") == "abababababab"
assert extractChangesetFromMessage("x 12345:123412341234") == "123412341234"
assert extractChangesetFromMessage("12345:abababababab y") == "abababababab"

def makeShell(shellCacheDir, sourceDir, archNum, compileType, valgrindSupport, currRev):
    tempDir = tempfile.mkdtemp(prefix="abc-" + currRev + "-")
    compileJsSrcPath = normExpUserPath(os.path.join(tempDir, 'compilePath', 'js', 'src'))

    vdump("Compiling in " + tempDir)

    # Copy the js tree.
    jsSrcDir = normExpUserPath(os.path.join(sourceDir, 'js', 'src'))
    if sys.version_info >= (2, 6):
        shutil.copytree(jsSrcDir, compileJsSrcPath,
                        ignore=shutil.ignore_patterns(
                            # ignore_patterns does not work in Python 2.5.
                            'jit-test', 'tests', 'trace-test', 'xpconnect'))
    else:
        shutil.copytree(jsSrcDir, compileJsSrcPath)
    jsPubSrcDir = normExpUserPath(os.path.join(sourceDir, 'js', 'public'))
    if os.path.isdir(jsPubSrcDir):
        shutil.copytree(jsPubSrcDir, os.path.join(compileJsSrcPath, '..', 'public'))
    mfbtSrcDir = normExpUserPath(os.path.join(sourceDir, 'mfbt'))
    if os.path.isdir(mfbtSrcDir):
        shutil.copytree(mfbtSrcDir, os.path.join(compileJsSrcPath, '..', '..', 'mfbt'))

    # Run autoconf.
    autoconfRun(compileJsSrcPath)

    # Create objdir within the compileJsSrcPath.
    objdir = os.path.join(compileJsSrcPath, compileType + '-objdir')
    os.mkdir(objdir)

    # Run configure.
    threadsafe = False  # Let's disable support for threadsafety in the js shell
    cfgPath = normExpUserPath(os.path.join(compileJsSrcPath, 'configure'))
    cfgJsBin(archNum, compileType, threadsafe, cfgPath, objdir)

    # Compile and copy the first binary.
    # Only pymake was tested on Windows.
    usePymake = True if platform.system() == 'Windows' else False
    try:
        shell = compileCopy(archNum, compileType, currRev, usePymake, sourceDir, shellCacheDir, objdir, valgrindSupport)
    finally:
        assert os.path.isdir(tempDir) is True
        rmDirInclSubDirs(tempDir)
        assert os.path.isdir(tempDir) is False
    return shell

# Run the testcase on the compiled js binary.
def testBinary(shell, file, flagsRequired, valgSupport):
    testBinaryCmd = [shell] + flagsRequired + [file]
    if valgSupport:
        testBinaryCmd = ['valgrind'] + testBinaryCmd
    vdump('The testing command is:' + ' '.join(testBinaryCmd))

    # Capture stdout and stderr into the same string.
    p = subprocess.Popen(testBinaryCmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    out, err = p.communicate()
    retCode = p.returncode
    if verbose:
        print 'The exit code is:', retCode
        if len(out) > 0:
            print 'stdout shows:', out
        if len(err) > 0:
            print 'stderr shows:', err

    # Switch to interactive input mode similar to `cat testcase.js | ./js -j -i`.
    # Doesn't work, stdout shows:
    #can't open : No such file or directory
    #The exit code is: 4
    #The second output is: None
    #if retCode == 0:
    #    # Append the quit() function to make the testcase quit.
    #    # Doesn't work if retCode is something other than 0, that watchExitCode specified.
    #    testcaseFile = open(file, 'a')
    #    testcaseFile.write('\nquit()\n')
    #    testcaseFile.close()
    #
    #    # Test interactive input.
    #    print 'Switching to interactive input mode in case passing as a CLI ' + \
    #            'argument does not reproduce the issue..'
    #    testBinaryCmd3 = subprocess.Popen([shell, methodJit, tracingJit, '-i'],
    #        stdin=(subprocess.Popen(['cat', file])).stdout)
    #    output2 = testBinaryCmd3.communicate()[0]
    #    retCode = testBinaryCmd3.returncode
    #    print 'The exit code is:', retCode
    #    print 'The second output is:', output2
    return out + "\n" + err, retCode

def bisectLabel(hgLabel, currRev, startRepo, endRepo, ignoreResult):
    '''Tell hg what we learned about the revision.'''
    assert hgLabel in ("good", "bad", "skip")

    outputResult = captureStdout(hgPrefix + ['bisect', '-U', '--' + hgLabel, currRev])[0]
    outputLines = outputResult.split("\n")

    if re.compile("Due to skipped revisions, the first (good|bad) revision could be any of:").match(outputLines[0]):
        print outputResult
        return None, None, None, startRepo, endRepo

    r = re.compile("The first (good|bad) revision is:")
    m = r.match(outputLines[0])
    if m:
        print '\nautoBisect shows this is probably related to the following changeset:\n'
        print outputResult
        blamedGoodOrBad = m.group(1)
        blamedRev = extractChangesetFromMessage(outputLines[1])
        return None, blamedGoodOrBad, blamedRev, startRepo, endRepo

    if ignoreResult:
        return None, None, None, startRepo, endRepo

    # e.g. "Testing changeset 52121:573c5fa45cc4 (440 changesets remaining, ~8 tests)"
    vdump(outputLines[0])

    currRev = extractChangesetFromMessage(outputLines[0])
    if currRev is None:
        raise Exception("hg did not suggest a changeset to test!")

    # Update the startRepo/endRepo values.
    start = startRepo
    end = endRepo
    if hgLabel == 'bad':
        end = currRev
    elif hgLabel == 'good':
        start = currRev
    elif hgLabel == 'skip':
        pass

    return currRev, None, None, start, end

def firstLine(s):
    return s.split('\n')[0]

# This function removes a directory along with its subdirectories.
def rmDirInclSubDirs(dir):
    #print 'Removing ' + dir
    shutil.rmtree(dir)

def lockedMain():
    """Prevent running two instances of autoBisect at once, because we don't want to confuse hg."""
    lockDir = os.path.join(shellCacheDir, "autobisect-lock")
    try:
        os.mkdir(lockDir)
    except OSError, e:
        print "autoBisect is already running"
        return
    try:
        main()
    finally:
        os.rmdir(lockDir)

if __name__ == '__main__':
    # Reopen stdout, unbuffered.
    sys.stdout = os.fdopen(sys.stdout.fileno(), 'w', 0)
    lockedMain()