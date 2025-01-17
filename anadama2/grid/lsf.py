# -*- coding: utf-8 -*-
import os
import sys
import time
import tempfile
import getpass

import six

from .grid import Grid
from .grid import GridWorker
from .grid import GridQueue

from .. import runners
from .. import picklerunner
from ..util import underscore
from ..util import find_on_path
from ..util import keyrename, keepkeys

if os.name == 'posix' and sys.version_info[0] < 3:
    import subprocess32 as subprocess
else:
    import subprocess

class LSF(Grid):
    """This class enables the Workflow class to dispatch tasks to
    LSF and its lookalikes. Use it like so:

    .. code:: python

      from anadama2 import Workflow
      from anadama2.lsf import LSF

      ctx = Workflow(grid_powerup=LSF(queue="general"))
      ctx.do("wget "
             "ftp://public-ftp.hmpdacc.org/"
             "HMMCP/finalData/hmp1.v35.hq.otu.counts.bz2 "
             "-O @{input/hmp1.v35.hq.otu.counts.bz2}")

      # run on sge with 200 MB of memory, 4 cores, and 60 minutes
      t1 = ctx.grid_do("pbzip2 -d -p 4 < #{input/hmp1.v35.hq.otu.counts.bz2} "
                       "> @{input/hmp1.v35.hq.otu.counts}",
                       mem=200, cores=4, time=60)

      # run on sge on the serial_requeue queue
      ctx.grid_add_task("some_huge_analysis {depends[0]} {targets[0]}",
                        depends=t1.targets, targets="output.txt",
                        mem=4000, cores=1, time=300, partition="serial_requeue")


      ctx.go()


    :param partition: The name of the SLURM partition to submit tasks to
    :type partition: str

    :param tmpdir: A directory to store temporary files in. All
      machines in the cluster must be able to read the contents of
      this directory; uses :mod:`anadama2.picklerunner` to create
      self-contained scripts to run individual tasks and calls
      ``srun`` to run the script on the cluster.
    :type tmpdir: str

    :keyword benchmark_on: Option to turn on/off benchmarking
    : type benchmark_on: bool

    :keyword options: Grid specific options to apply to each job
    :type options: str

    """

    def __init__(self, partition, tmpdir, benchmark_on=None, options=None, environment=None):
        super(LSF, self).__init__("lsf", GridWorker, LSFQueue(partition, benchmark_on, options, environment), tmpdir, benchmark_on)


class LSFQueue(GridQueue):

    def __init__(self, partition, benchmark_on=None, options=None, environment=None):
        super(LSFQueue, self).__init__(partition, benchmark_on)
        self.options=options
        self.environment=environment

        self.job_code_completed="DONE"
        self.job_code_error="EXIT"
        self.job_code_terminated="EXIT"
        self.job_code_deleted="EXIT"

        self.all_failed_codes=[self.job_code_error,self.job_code_terminated,self.job_code_deleted]
        self.all_stopped_codes=[self.job_code_completed]+self.all_failed_codes

        # allow for jobs to be terminated when about to reach the memory requested
        self.memory_buffer = 1024
        self.mem_per_core = True

    @staticmethod
    def submit_command(grid_script):
        return {"cmd":"bsub",  "script":grid_script}

    def submit_template(self):
        template = [
            "#BSUB -J anadama_job",
#            "#BSUB -q ${partition}",
            "#BSUB -n ${cpus}",
            "#BSUB -W ${time}",
            "#BSUB -R 'rusage[mem=${memory}MB]'",
            "#BSUB -o ${output}",
            "#BSUB -e ${error}"]

        # add user supplied options if provided
        if self.options:
            template+=["#BSUB "+option for option in self.options]

        # add user supplied environment commands if provided
        if self.environment:
            template+=self.environment

        return template

    def job_failed(self,status):
        # check if the job has a status that it failed
        return True if status in self.all_failed_codes else False

    def job_stopped(self,status):
        # check if the job has a status which indicates it stopped running
        return True if status in self.all_stopped_codes else False

    def job_memkill(self, status, jobid, memory):
        # check if the job was killed because it used too much memory
        new_status, cpus, new_time, new_memory = self.get_benchmark(jobid)

        # if memory is not yet available for the job, wait for a new benchmark
        if new_memory == "NA":
            new_status, cpus, new_time, new_memory = self.get_benchmark(jobid, wait=True)
        if type(memory) is list:
            memory = memory[0]
        try:
            exceed_allocation = True if (float(new_memory) + self.memory_buffer) > float(memory) else False
        except ValueError:
            exceed_allocation = False

        return True if exceed_allocation and new_status == self.job_code_terminated else False

    def job_timeout(self, status, jobid, time):
        # check if the job was killed because it used too much memory
        new_status, cpus, new_time, new_memory = self.get_benchmark(jobid)

        # if time is not yet available for the job, wait for a new benchmark
        if new_time == "NA":
            new_status, cpus, new_time, new_memory = self.get_benchmark(jobid, wait=True)
        if type(time) is list:
            time = time[0]
        try:
            exceed_allocation = True if float(new_time) > float(time) else False
        except ValueError:
            exceed_allocation = False

        return True if exceed_allocation and new_status == self.job_code_terminated else False

    def refresh_queue_status(self):
        """ Get the latest status for the grid jobs using the same command for
        jobs in the queue and for completed jobs to benchmark """

        # Get the jobid and state for all jobs pending/running/completed for the current user
        fmt = 'jobid jobindex stat job_name kill_reason exit_reason exit_code start_time run_time slots max_mem  delimiter="\t"'
        qacct_stdout=self.run_grid_command_resubmit(["bjobs","-a", "-noheader", "-o", fmt])
        #qacct_stdout=self.run_grid_command_resubmit(["bacct","-u",getpass.getuser()])
        #print(qacct_stdout)
        # info list should include jobid, state, cpus, time, and maxrss
        info=[]
        job_status=[]

        for line in qacct_stdout.split("\n"):
            # get rid of header and unrelated jobs
            if "anadama_job" in line:
                linesp = line.split("\t")
                if linesp[2] == "EXIT":
                    reason_or_status = "EXIT:" + linesp[6]
                else:
                    reason_or_status = linesp[2]
                try:
                    job_status = [
                        linesp[0], # jobid
                        reason_or_status, # status
                        linesp[9], # slots
                        float(linesp[8].split(" ")[0])/60.0, # time
                        linesp[10].replace("bytes", "") # mem
                    ]
                except Exception as e:
                    print("Error parsing job status")
                    print(e)
                    job_status = [linesp[0], "NA", "NA", "NA"]
            # if line.startswith("jobnumber") or line.startswith("job_number"):
            #     if job_status:
            #         info.append(job_status)
            #     job_status=[line.rstrip().split()[-1],"NA","NA","NA","NA"]
            # # get the states for completed jobs
            # elif line.startswith("failed"):
            #     failed_code = line.rstrip().split()[1]
            #     if failed_code != "0":
            #         if failed_code in ["37","100"]:
            #             job_status[1]=self.job_code_terminated
            #         else:
            #             job_status[1]=self.job_code_error
            # elif line.startswith("deleted_by"):
            #     if line.rstrip().split()[-1] != "NONE" and job_status[1] == self.job_code_terminated:
            #         job_status[1]=self.job_code_deleted
            # elif line.startswith("exit_status"):
            #     # only record if status has not yet been set
            #     if job_status[1] == "NA":
            #         exit_status = line.rstrip().split()[-1]
            #         if exit_status == "0":
            #             job_status[1]=self.job_code_completed
            #         elif exit_status == "137":
            #             job_status[1]=self.job_code_terminated
            #         else:
            #             job_status[1]=self.job_code_error
            # # get the current state for running jobs
            # elif line.startswith("job_state"):
            #     job_status[1]=line.rstrip().split()[-1]
            # elif line.startswith("slots"):
            #     job_status[2]=line.rstrip().split()[-1]
            # elif line.startswith("ru_wallclock"):
            #     try:
            #         # get the elapsed time in minutes
            #         job_status[3]=str(float(line.rstrip().split()[-1])/60.0)
            #     except ValueError:
            #         job_status[3]="NA"
            # elif line.startswith("ru_maxrss"):
            #     job_status[4]=line.rstrip().split()[-1]+"K"

            if job_status:
                info.append(job_status)

        return info
