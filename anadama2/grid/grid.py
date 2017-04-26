# -*- coding: utf-8 -*-

import os
import sys
import threading
import Queue
import time
import tempfile
import string
import datetime
import logging
import itertools

import six

from .. import runners
from ..helpers import format_command

if os.name == 'posix' and sys.version_info[0] < 3:
    import subprocess32 as subprocess
else:
    import subprocess

class GridJobRequires(object):
    """Defines the resources required for a task on the grid.

    :param time: Wall clock time in minutes.
    :type time: int

    :param mem: RAM Usage in MB (8*1024*1024 bits).
    :type mem: int

    :param cores: CPU cores.
    :type cores: int
    """
    
    def __init__(self, time, mem, cores, depends=None):
        # if time is not an int, try to format the equation
        if not str(time).isdigit():
            self.time = format_command(time, depends=depends, cores=cores)
        else:
            self.time = int(time)
        
        # if memory is not an int, try to format the equation
        if not str(mem).isdigit():
            self.mem = format_command(mem, depends=depends, cores=cores) 
        else:
            self.mem = int(mem)
            
        self.cores = int(cores) 
    
class Grid(object):
    """ Base Grid Workflow manager class """
    
    def __init__(self, name, worker, queue, partition, tmpdir, benchmark_on=None):
        self.name = name
        self.worker = worker
        self.queue = queue
        self.partition = partition
        self.tmpdir = tmpdir
        # create the folder if it does not already exist for temp directory
        if not os.path.isdir(self.tmpdir):
            os.makedirs(self.tmpdir)
        
        self.task_data = dict()

    def _get_grid_task_settings(self, kwargs, depends):
        """ Get the resources required to run this task on the grid """
        
        # check for the required keywords
        requires=[]
        for key in ["time","mem","cores"]:
            try:
                requires.append(kwargs[key])
            except KeyError:
                raise KeyError(key+" is a required keyword argument for a grid task")
        requires+=[depends]
        
        return (GridJobRequires(*requires), self.partition, self.tmpdir)
        
    def do(self, task, **kwargs):
        """Accepts the following extra arguments:
        
        :param time: The maximum time in minutes allotted to run the
          command
        :type time: int

        :param mem: The maximum memory in megabytes allocated to run
          the command
        :type mem: int

        :param cores: The number of CPU cores allocated to the job
        :type cores: int

        :param partition: The grid partition to send this job to
        :type partition: str
        """
        
        self.add_task(task, **kwargs)


    def add_task(self, task, **kwargs):
        """Accepts the following extra arguments:
        
        :keyword time: The maximum time in minutes allotted to run the
          command
        :type time: int

        :keyword mem: The maximum memory in megabytes allocated to run
          the command
        :type mem: int

        :keyword cores: The number of CPU cores allocated to the job
        :type cores: int

        :keyword partition: The grid partiton to send this job to
        :type partition: str
        """
        
        self.task_data[task.task_no] = self._get_grid_task_settings(kwargs, task.depends)


    def runner(self, workflow, jobs=1, grid_jobs=1):
        runner = runners.GridRunner(workflow)
        runner.add_worker(runners.ParallelLocalWorker,
                          name="local", rate=jobs, default=True)
        runner.add_worker(self.worker, name=self.name, rate=grid_jobs)
        runner.routes.update((
            ( task_no, (self.name, list(extra)+[self.queue, workflow._reporter]) )
            for task_no, extra in six.iteritems(self.task_data)
        ))
        return runner   

class GridQueue(object):
    
    def __init__(self, benchmark_on=None):
        # this is the refresh rate for checking the queue, in seconds
        self.refresh_rate = 10*60

        # this is the rate for checking the job status, in seconds
        self.check_job_rate = 60
        
        # this is the number of minutes to wait if there is an time out
        # socket error returned from the scheduler when running a command
        self.timeout_sleep = 5*60
        
        # this is the number of times to retry after a timeout error
        self.timeout_retry_max = 3
        
        # this is the number of seconds to wait after job submission
        self.submit_sleep = 5
        
        # this is the last time the queue was checked
        self.last_check = time.time()
        self.sacct = None
        
        # create a lock for jobs in queue
        self.lock_status = threading.Lock()
        self.lock_submit = threading.Lock()
        
        # set if benchmarking should be run
        self.benchmark_on = benchmark_on
        
    @staticmethod
    def submit_command(grid_script):
        raise NotImplementedError
    
    @staticmethod
    def submit_template():
        raise NotImplementedError
    
    @staticmethod
    def job_failed(status):
        raise NotImplementedError
    
    @staticmethod
    def job_stopped(status):
        raise NotImplementedError
    
    @staticmethod
    def job_memkill(status):
        raise NotImplementedError
        
    @staticmethod
    def job_timeout(status):
        raise NotImplementedError
    
    @staticmethod
    def get_job_status_from_stderr(error_file, grid_job_status):
        raise NotImplementedError
    
    def refresh_queue_status(self, benchmarking):
        raise NotImplementedError
    
    def get_queue_status(self, benchmarking=None, refresh=None):
        """ Get the queue accounting stats """
        
        # lock to prevent race conditions with status update
        self.lock_status.acquire()
        
        # check the last time the queue was captured and refresh if set
        current_time = time.time()
        if ( current_time - self.last_check > self.refresh_rate ) or refresh or self.sacct is None:
            self.last_check = current_time
            logging.info("Getting latest queue info to refresh job status")
            self.sacct = self.refresh_queue_status(benchmarking)
            
        self.lock_status.release()
        
        return self.sacct
    
    def get_all_stats_for_jobid(self,jobid,benchmarking=None):
        """ Get all the stats for a specific job id """
        
        # use the existing stats, to get the information for the jobid
        job_stats=list(filter(lambda x: x[0].startswith(jobid),self.get_queue_status(benchmarking)))
       
        # if the job stats are not found for the job, return an NA state
        if not job_stats:
            job_stats=[[jobid,"Waiting","NA","NA","NA"]] 

        return job_stats
    
    def get_job_status(self, jobid, benchmarking=None):
        """ Check the status of the job """
        
        info=self.get_all_stats_for_jobid(jobid, benchmarking)
        
        return info[0][1]

    def record_benchmark(self, jobid, task_number, reporter):
        """ Check the benchmarking stats of the grid id """
        
        # check if benchmarking is set to off
        if not self.benchmark_on:
            logging.info("Benchmarking is set to off")
            return
            
        reporter.task_grid_status(task_number,jobid,"Getting benchmarking data")
        # if the job is not shown to have finished running then
        # wait for the next queue refresh
        status=self.get_job_status(jobid, benchmarking=True)
        if not (self.job_stopped(status) or self.job_failed(status)):
            wait_time = abs(self.refresh_rate - (time.time() - self.last_check)) + 10
            time.sleep(wait_time)

        info=self.get_all_stats_for_jobid(jobid)
        
        try:
            status=info[0][1]
        except IndexError:
            status="Unknown"

        try:
            cpus=info[0][2]
        except IndexError:
            cpus="NA"
    
        try:
            elapsed=info[0][3]
        except IndexError:
            elapsed="NA"

        # get the memory max from the batch line which is the second line of output
        try:
            memory=info[1][4]
        except IndexError:
            memory="NA"
        
        if "K" in memory:    
            # if memory is in KB, convert to MB
            memory="{:.1f}".format(float(memory.replace("K",""))/1024.0)
        elif "M" in memory:
            memory="{:.1f}".format(float(memory.replace("M","")))
        elif "G" in memory:
            # if memory is in GB, convert to MB
            memory="{:.1f}".format(float(memory.replace("G",""))*1024.0)
            
        logging.info("Benchmark information for job id %s:\nElapsed Time: %s \nCores: %s\nMemory: %s MB",
            task_number, elapsed, cpus, memory)   
        
        reporter.task_grid_status(task_number,jobid,"Final status of "+status)
    
    def run_grid_command(self,command):
        """ Run the grid command and check for errors """
        
        error=None
        try:
            logging.debug("Running grid command: %s"," ".join(command))
            stdout=subprocess.check_output(command, stderr=subprocess.STDOUT)
        except subprocess.CalledProcessError as err:
            error=err.output
            stdout="error"
            
        timeout_error=False
        if error and "error" in error and "Socket timed out on send/recv operation" in error:
            # check for a socket timeout error
            timeout_error=True
            
        return stdout, timeout_error
    
    def run_grid_command_resubmit(self,command):
        """ Run this grid command, check for error, resubmit if needed """
        
        # run the grid command
        stdout, timeout_error = self.run_grid_command(command)
        
        # retry if timeout error present after wait
        resubmissions = 0
        if timeout_error and resubmissions < self.timeout_retry_max:
            resubmissions+=1
            # wait before retrying
            logging.warning("Unable to run grid command, waiting and retrying")
            time.sleep(self.timeout_sleep)
            stdout, timeout_error = self.run_grid_command(command)
        
        return stdout
    
    @staticmethod
    def job_submission_failed(jobid):
        """ Check if the job failed in submission and did not get an id """
        return True if not jobid.isdigit() else False

    def submit_job(self,grid_script):
        """ Submit the grid jobs and return the grid job id """
        
        # lock so only one task submits jobs to the queue at a time
        self.lock_submit.acquire()

        # submit the job and get the grid id
        logging.debug("Submitting job to grid")
        stdout=self.run_grid_command_resubmit(self.submit_command(grid_script))
        
        try:
            jobid=stdout.rstrip().split()[-1]
        except IndexError:
            jobid="error"
        
        # check the jobid for a submission failed
        if self.job_submission_failed(jobid):
            logging.error("Unable to submit job to queue")
        
        # pause for the scheduler
        time.sleep(self.submit_sleep)
        
        self.lock_submit.release()
    
        return jobid
    
    def create_grid_script(self,partition,cpus,minutes,memory,command,taskid,dir):
        """ Create a grid script from the template also creating temp stdout and stderr files """
    
        # create temp files for stdout, stderr, and return code    
        handle_out, out_file=tempfile.mkstemp(suffix=".out",prefix="task_"+str(taskid)+"_",dir=dir)
        os.close(handle_out)
        handle_err, error_file=tempfile.mkstemp(suffix=".err",prefix="task_"+str(taskid)+"_",dir=dir)
        os.close(handle_err)
        handle_rc, rc_file=tempfile.mkstemp(suffix=".rc",prefix="task_"+str(taskid)+"_",dir=dir)
        os.close(handle_rc)
        
        # add the remaining sections to the bash template
        bash_template = string.Template("\n".join(["#!/bin/bash "] + self.submit_template() + ["", "${command}", "${rc_command}"]))
    
        # convert the minutes to the time string "D-HH:MM:SS"
        time=str(datetime.timedelta(minutes=minutes)).replace(' day, ','-').replace(' days, ','-')
    
        bash=bash_template.substitute(partition=partition,cpus=cpus,time=time,
            memory=memory,command=command,output=out_file,error=error_file,rc_command="export RC=$? ; echo $RC > "+rc_file+" ; bash -c 'exit $RC'")
        file_handle, new_file=tempfile.mkstemp(suffix=".bash",prefix="task_"+str(taskid)+"_",dir=dir)
        os.write(file_handle,bash)
        os.close(file_handle)
        
        return new_file, out_file, error_file, rc_file


class GridWorker(threading.Thread):
    """ Base Grid Worker class """
    
    def __init__(self, work_q, result_q, lock, reporter):
        super(GridWorker, self).__init__()
        self.daemon = True
        self.logger = runners.logger
        self.work_q = work_q
        self.result_q = result_q
        self.lock = lock
        self.reporter = reporter    
    
    @staticmethod
    def appropriate_q_class(*args, **kwargs):
        return six.moves.queue.Queue(*args, **kwargs)    

    @staticmethod
    def appropriate_lock():
        return threading.Lock() 
    
    def run(self):
        return runners.worker_run_loop(self.work_q, self.result_q, self.run_task_by_type)
    
    @staticmethod
    def run_task_function(task, extra):
        raise NotImplementedError
   
    @classmethod 
    def run_task_by_type(cls, task, extra):
        # if the task is a function, then use pickle srun interface
        if six.callable(task.actions[0]):
            return cls.run_task_function(task, extra)
        else:
            return cls.run_task_command(task, extra)   
        
    @classmethod
    def run_task_command(cls, task, extra):
        (perf, partition, tmpdir, grid_queue, reporter) = extra
        # report the task has started
        reporter.task_running(task.task_no)
        
        # create a slurm script and stdout/stderr files for this task
        commands="\n".join(task.actions)
        logging.info("Running commands for task id %s:\n%s", task.task_no, commands)
    
        resubmission = 0    
        cores, time, memory = perf.cores, perf.time, perf.mem
    
        jobid, out_file, error_file, rc_file = cls.submit_grid_job(cores, time, memory, 
            partition, tmpdir, commands, task, grid_queue, reporter)
    
        # monitor job if submission was successful
        result, job_final_status = cls.check_submission_then_monitor_grid_job(grid_queue, 
            task, jobid, out_file, error_file, rc_file, reporter)
    
        # if a timeout or memory max, resubmit at most three times
        while ( grid_queue.job_timeout(job_final_status) or grid_queue.job_memkill(job_final_status) ) and resubmission < 3:
            reporter.task_grid_status(task.task_no,jobid,"Resubmitting due to "+job_final_status)
            resubmission+=1
            # increase the memory or the time
            if grid_queue.job_timeout(job_final_status):
                time = time * 2
                logging.info("Resubmission number %s of grid job for task id %s with 2x more time: %s minutes", 
                    resubmission, task.task_no, time)
            elif grid_queue.job_memkill(job_final_status):
                memory = memory * 2
                logging.info("Resubmission number %s of grid job for task id %s with 2x more memory: %s MB",
                    resubmission, task.task_no, memory)
            
            jobid, out_file, error_file, rc_file = cls.submit_grid_job(cores, time, memory,
                partition, tmpdir, commands, task, grid_queue, reporter)
    
            # monitor job if submission was successful
            result, job_final_status = cls.check_submission_then_monitor_grid_job(grid_queue, 
                task, jobid, out_file, error_file, rc_file, reporter)
                
        # get the benchmarking data if the job was submitted
        if not grid_queue.job_submission_failed(jobid):
            grid_queue.record_benchmark(jobid, task.task_no, reporter)
        
        return result
    
    @classmethod
    def submit_grid_job(cls, cores, time, memory, partition, tmpdir, commands, task, grid_queue, reporter):
        
        # evaluate the time/memory requests for the job
        time, memory = cls.evaluate_resource_requests(time, memory)
        
        # create the grid bash script
        grid_script, out_file, error_file, rc_file = grid_queue.create_grid_script(partition,
            cores, time, memory, commands, task.task_no, tmpdir)
    
        logging.info("Created grid files for task id %s: %s, %s, %s, %s",
            task.task_no, grid_script, out_file, error_file, rc_file)
    
        # submit the job
        jobid = grid_queue.submit_job(grid_script)
        
        logging.info("Submitted job for task id %s: grid id %s", task.task_no,
            jobid)
        
        if not grid_queue.job_submission_failed(jobid):
            reporter.task_grid_status(task.task_no,jobid,"Submitted")
       
        return jobid, out_file, error_file, rc_file
    
    @staticmethod
    def log_grid_output(taskid, file, file_type):
        """ Write the grid stdout/stderr files to the log """
        
        try:
            lines=open(file).readlines()
        except EnvironmentError:
            lines=[]
            
        logging.info("Grid %s from task id %s:\n%s",taskid, file_type, "".join(lines))
        
    @staticmethod
    def get_return_code(file):
        """ Read the return code from the file """
    
        try:
            line=open(file).readline().rstrip()
        except EnvironmentError:
            line=""
    
        return line
    
    @staticmethod
    def evaluate_resource_requests(time,mem):
        """ Evaluate the time/memory requests for the grid job, allowing for ints or formulas """
        
        try:
            time=eval(str(time))
        except TypeError:
            raise TypeError("Unable to evaluate time request for task: "+ time)
        
        try:
            mem=eval(str(mem))
        except TypeError:
            raise TypeError("Unable to evaluate memory request for task: "+ mem)
        
        return time, mem  
    
    @classmethod
    def check_submission_then_monitor_grid_job(cls, grid_queue, task, grid_jobid, 
        out_file, error_file, rc_file, reporter):
        
        # monitor job if submission was successful
        if not grid_queue.job_submission_failed(grid_jobid):
            result, job_final_status = cls.monitor_grid_job(grid_queue, task, grid_jobid,
                out_file, error_file, rc_file, reporter)
        else:
            job_final_status = "SUBMIT FAILED"
            # get the anadama task result
            result=runners._get_task_result(task)
            # add the extra error
            result = result._replace(error=str(result.error)+"Unable to submit job to queue.")
            
        return result, job_final_status
   
    @classmethod 
    def monitor_grid_job(cls, grid_queue, task, grid_jobid, out_file, error_file, rc_file, reporter): 
        # poll to check for status
        grid_job_status=None
        for tries in itertools.count(1):
            # only check status at intervals
            time.sleep(grid_queue.check_job_rate)
            
            # check the queue stats
            grid_job_status = grid_queue.get_job_status(grid_jobid)
            reporter.task_grid_status_polling(task.task_no,grid_jobid,grid_job_status)
            
            logging.info("Status for job id %s with grid id %s is %s",task.task_no,
                grid_jobid,grid_job_status)
            
            if grid_queue.job_stopped(grid_job_status):
                logging.info("Grid status for job id %s shows it has stopped",task.task_no)
                break
            
            # check if the return code file is written
            if os.path.getsize(rc_file) > 0:
                logging.info("Return code file for job id %s shows it has stopped",task.task_no)
                break
            
        # check if a grid error is written to the output file
        grid_job_status = grid_queue.get_job_status_from_stderr(error_file, grid_job_status)
        
        # write the stdout and stderr to the log
        cls.log_grid_output(task.task_no, out_file, "standard output")
        cls.log_grid_output(task.task_no, error_file, "standard error")
        cls.log_grid_output(task.task_no, rc_file, "return code")
        
        # check the return code
        extra_error=""
        return_code=cls.get_return_code(rc_file)
        if return_code and not return_code == "0":
            extra_error="\nReturn Code Error: " + return_code
          
        # check the queue status
        if grid_queue.job_failed(grid_job_status):
            extra_error+="\nGrid Status Error: " + grid_job_status
     
        # get the anadama task result
        result=runners._get_task_result(task)
    
        # add the extra error if found
        if extra_error:
            result = result._replace(error=str(result.error)+extra_error)
     
        return result, grid_job_status
    
    