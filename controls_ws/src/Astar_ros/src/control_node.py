#!/usr/bin/env python
# -*- coding: utf-8 -*-
import rospy
from std_msgs.msg import Float64MultiArray
from std_msgs.msg import Float32
# from geometry_msgs.msg import PoseStamped
# from nav_msgs.msg import OccupancyGrid
from nav_msgs.msg import Path
from std_msgs.msg import String
from std_msgs.msg import Bool
from nav_msgs.msg import Path
from std_msgs.msg import Int8
from std_msgs.msg import Float64
from geometry_msgs.msg import Point
from geometry_msgs.msg import Pose2D
from sensor_msgs.msg import Imu
from io import StringIO
import numpy as np
import sys
import os
import do_mpc
from do_mpc.data import save_results, load_results
from casadi import *
import matplotlib.pyplot as plt
# import matplotlib as mpl
import scipy.interplotate as interp
import pickle
sys.path.append('../../')


class MPController:
    def __init__(self, N_ref = 100):

        #Model and MPC variables
        self.model_type= 'continuous'	
        self.J=375            # moment of interia
        self.La=1.2           # distance of front tires from COM in m
        self.Lb=1.2           # distance of back tires from COM in m
        self.m=200            # mass of vehicle in kg
        self.Cy=0.1           # Tyre stiffness constant
        self.t_s=0.1          # sample time
        self.N=70             # Horizon
        self.N_ref = N_ref    # control iterations


        # variables for control execution
        self.velocities=[1.5]												# array to store velocities
        self.path_points=[]                                        		    # array to store the path
        self.c=1
        self.packed=[velocities,c]
        self.x_initial=vertact([0],[0],[0],[1.5],[0],[0],[0],[0],[0])       # intial state vector
        self.state = self.x_initial 										# variable to store the curent state vector 

        #MPC  variables
        self.model = self.mpc_model()
        self.controller = self.controller(self.model)
        self.simulator = self.mpc_simulator(self.model)
        self.estimator = self.do_mpc.estimator.StateFeedback(self.model)

        # ROS variables
        self.acc_pub = rospy.Publisher('acceleration', Float32, queue_size=10)
        self.brake_pub = rospy.Publisher('brake', Float32, queue_size=10)
        self.steer_pub = rospy.Publisher('steer', Float32, queue_size=10)
        self.rate=rospy.Rate(10)
        self.vel_sub=rospy.Subscriber("/velocity_plan",Float64MultiArray,self.v_callback)
        self.path_sub=rospy.Subscriber("/A_star_path",Path, self.path_callback)
        # sels.state_sub = rospy.Subscriber('state', Float64MultiArray , self.state_callback)


    def mpc_model(self):
        # Obtain an instance of the do-mpc model class
        model = do_mpc.model.Model(self.model_type)

        # States variables of the model
        xc=model.set_variable(var_type='_x',var_name='xc',shape=(1,1))                     # x position
        yc=model.set_variable(var_type='_x',var_name='yc',shape=(1,1))                     # y position
        v=model.set_variable(var_type='_x',var_name='v',shape=(1,1))                       # velocity
        theta=model.set_variable(var_type='_x',var_name='theta',shape=(1,1))               # yaw angle
        phi=model.set_variable(var_type='_x',var_name='phi',shape=(1,1))                   # yaw angular velocity
        delta=model.set_variable(var_type='_x',var_name='delta',shape=(1,1))               # steering angle
        

        # virtual state for timing law
        z = model.set_variable(var_type='_x', var_name='z', shape = (1,1))


        #control inputs
        a=model.set_variable(var_type='_u',var_name='a',shape=(1,1))    # acceleration
        w=model.set_variable(var_type='_u',var_name='w',shape=(1,1))    # steering rate (angular)


        # virtual control input for timing law
        u_v = model.set_variable(var_type='_u',var_name='u_v', shape=(1,1))


        # Set right-hand-side of ODE for all introduced states (_x).
        # Names are inherited from the state definition.
        
        Fyf=self.Cy*(delta-(self.La*phi)/v)
        Fyr=(Cy*self.Lb*phi)/v
        
        equations=vertcat( v*np.sin(theta), 
                            v*np.cos(theta),
                            a_s* np.cos(delta)-(2.0/m)*Fyf*np.sin(delta),
                            phi,
                            (1.0/self.J)*(self.La*(m*a*np.sin(delta)+2*Fyf*np.cos(delta))-2*self.Lb*Fyr),
                            w
                            )
        
        
        
        model.set_rhs('xc',equations[0])
        model.set_rhs('yc',equations[1])
        model.set_rhs('v',equations[2])
        model.set_rhs('theta',equations[3])
        model.set_rhs('phi',equations[4])
        model.set_rhs('delta',equations[5])

        #timing law equation
        timing_law = u_v
        
        #RHS of timing law ODEs
        model.set_rhs('z', timing_law)

        # Setup model:
        model.setup()

        return model

    def controller(self, model):
        # Obtain an instance of the do-mpc MPC class
        # and initiate it with the model:
        mpc = do_mpc.controller.MPC(model)

        # Set parameters:
        setup_mpc = {
            'n_horizon': self.N,
            't_step': self.t_s,
            'n_robust': 0,
            'open_loop':0,
            'state_discretization': 'collocation',
            'collocation_type': 'radau',
            'collocation_deg': 2,
            'collocation_ni': 2,
            'store_full_solution': True,
        }
        mpc.set_param(**setup_mpc)
        
        

        xc = model.x['xc']
        yc = model.x['yc']
        z = model.x['z']
            
        # Objective function:
        cost = (xc-self.x_z(z))**2 + (yc-self.y_z(z))**2 + (model.x['v']-self.v_z(z))**2


        mterm = cost # terminal cost
        lterm = cost # stage cost

        mpc.set_objective(mterm=mterm, lterm=lterm)
        mpc.set_rterm(a=0.00005)
        mpc.set_rterm(w=0.00001) # Scaling for quad. cost.

        ####################### State and input bounds #######################3

        # mpc.bounds['lower','_x','xc']=x_0[0]-1e-4
        # mpc.bounds['lower','_x','yc']=y_lower
        mpc.bounds['lower','_x','v']=0 #max reverse speed in m/s
        mpc.bounds['lower','_x','theta']=-50
        mpc.bounds['lower','_x','phi']=-50
        mpc.bounds['lower','_x','delta']=-np.pi/6
        # mpc.bounds['upper','_x','xc']=target_x+0.1
        # mpc.bounds['upper','_x','yc']=y_upper
        mpc.bounds['upper','_x','v']=20 #max forward speed in m/s
        mpc.bounds['upper','_x','theta']=50
        mpc.bounds['upper','_x','phi']=50
        mpc.bounds['upper','_x','delta']=np.pi/6



        mpc.bounds['lower','_u','a']=-10
        mpc.bounds['lower','_u','w']=-10
        mpc.bounds['upper','_u','a']=10
        mpc.bounds['upper','_u','w']=10

        mpc.setup()

        return mpc

    def mpc_simulator(self, model):
        # Obtain an instance of the do-mpc simulator class
        # and initiate it with the model:
        simulator = do_mpc.simulator.Simulator(model)

        # Set parameter(s):
        simulator.set_param(t_step = self.t_s)

        # Setup simulator:
        simulator.setup()

        return simulator

    def control_loop(self):

        ################################################################################################
        # Set the initial conditions for first iteration
        self.simulator.x0 = self.x_initial	


        x_0 = self.simulator.x0.cat.full()

        self.controller.x0 = x_0
        self.estimator.x0 = x_0

        self.controller.set_initial_guess()
        self.simulator.set_initial_guess()
        self.controller.reset_history()
        self.simulator.reset_history()
        ################################################################################################

        # Defining arrays for state, path and velocity
        state = []
        x = []
        y = []
        v = []              

        steer = self.simulator.x0['delta']	
        # Start the control loop
        for k in range(self.N_ref):
            print('\n\n################################################    ' + str(k) + '    #########################################\n\n')
            
            rospy.spinOnce()                               # shifted from bottom to top, because we need to make the subsc call first		

            u0 = controller.make_step(x_0)     	    	   # Determine optimal control inputs using the inital state given

            # publish the steering angle and acceleration and brake values 
            if u0[0][0]>=0:
                self.acc_pub.publish(u0[0][0])
            else:
                self.brake_pub.publish((-1)*u0[0][0])
        
            steer += u[1][0]*self.t_s
            self.steer_pub.publish(steer)

            y_n = simulator.make_step(u0)					# Simulate the next step using the control inputs
            x_0= estimator.make_step(y_n)					# estimate the next state

        # plt.plot(x,y)

    def path_callback(self,path):

        path_repeat=False
        ##################################
        # Step1 - Obtaining the path points
        ##################################

        l=len(path.poses)                                                   # length of the path
        points=np.zeros((l,2))                                              # array for storing the path points
        # check for repetition on the path. 
        if len(path_points) > 0:                                            # Prevents check the first time this fucntion is called
            if self.path_points[0][0]==path.poses[0].pose.position.y:            # checks the y-coordinate of path
                print("###repetition detected###")
                path_repeat=True
                path_sub.unregister()                                       # unsubscribe the topic
                print("#######################################path unregistered#######################################")

        # after check passed
        if not path_repeat:    
            for j in range(l):
                points[j][0]=path.poses[j].pose.position.y
                points[j][1]=path.poses[j].pose.position.x
                self.path_points.append([path.poses[j].pose.position.y,path.poses[j].pose.position.x])


        ###########################
        # Step2 - Interplotate
        ###########################
        #Cubic Spline interpolation using the indices as the variable.
        z = range(0,l)
        # x_z = interp.CubicSpline(z, points[1])
        # y_z = interp.CubicSpline(z, points[0])

        x_z = interpolant('x_z', "bspline", [z], points[1,:])
        y_z = interpolant('y_z', "bspline", [z], points[0,:])



        ####################################
        # Step3 - Update the function handles
        ####################################
        self.set_path(x_z,y_z)


    def vel_callback(self,vel_arr):

        self.velocities.append(v_arr.data[0])
        ######################################
        # Step1 - Obtaining the velocity points
        ######################################
        vel=np.zeros((l,1))  
        l=len(v_arr.data)
        for i in range(l):
            vel[i][0] = vel_arr.data[i] 

        ###########################
        # Step2 - Interplotate
        ###########################
        #Cubic Spline interpolation using the indices as the variable.
        z = range(0,l)
        # v_z = interp.CubicSpline(z, vel)
        v_z = interpolant('v_z', "bspline", [z], vel)


        ####################################
        # Step3 - Update the function handles
        ####################################
        self.set_vel(v_z)




    def state_callback(self,state):
        # get the state 
        #update the clasas varibale for curent state value
        pass


    ############## Helper Functions ##############
    # function to fetch the current state
    def get_curr_state(self):
        return self.state

    # these set functions are used to assign the class private function handles to the calulated ones
    def set_path(self, x_z, y_z):
        self.x_z = x_z
        self.y_z = y_Z

    def set_vel(self, v_z):
        self.v_z = v_z
    ##############################################

    if __name__  == '__main__':

        # Create class object 
        controlObj = MPController()

        # run the control loop
        controlObj.control_loop()













    #def get_bezier_coef(points):
    #    n = len(points) - 1

    #    # build coefficents matrix
    #    C = 4 * np.identity(n)
    #    np.fill_diagonal(C[1:], 1)
    #    np.fill_diagonal(C[:, 1:], 1)
    #    C[0, 0] = 2
    #    C[n - 1, n - 1] = 7
    #    C[n - 1, n - 2] = 2

    #    # build points vector
    #    P = [2 * (2 * points[i] + points[i + 1]) for i in range(n)]
    #    P[0] = points[0] + 2 * points[1]
    #    P[n - 1] = 8 * points[n - 1] + points[n]

    #    # solve system, find a & b
    #    A = np.linalg.solve(C, P)
    #    B = [0] * n
    #    for i in range(n - 1):
    #        B[i] = 2 * points[i + 1] - A[i + 1]
    #    B[n - 1] = (A[n - 1] + points[n]) / 2

    #    return A, B


    ## returns the general Bezier cubic formula given 4 control points
    #def get_cubic(a, b, c, d):
    #    return lambda t: np.power(1 - t, 3)*a + 3*np.power(1 - t, 2)*t*b + 3*(1 - t)*np.power(t, 2)*c + np.power(t, 3)*d


    ## return one cubic curve for each consecutive points
    #def get_bezier_cubic(points):
    #    A, B = get_bezier_coef(points)
    #    return [
    #        get_cubic(points[i], A[i], B[i], points[i + 1])
    #        for i in range(len(points) - 1)
    #    ]


    #def derivative_bezier(a,b,c,d):
    #    return lambda t: np.power(1-t,2)*a*(-3)+3*b*(np.power(1-t,2)-2*t*(1-t))+3*c*(2*t*(1-t)-np.power(t,2))+3*d*np.power(t,2)


    #def derivative_list(points):
    #    A,B=get_bezier_coef(points)
    #    return [derivative_bezier(points[i],A[i],B[i],points[i+1]) for i in range(len(points)-1)]


    ## evalute each cubic curve on the range [0, 1] sliced in n points
    #def evaluate_bezier(points, n):
    #    curves = get_bezier_cubic(points)
    #    return np.array([fun(t) for fun in curves for t in np.linspace(0, 1, n)])


    #def trajectory_gen(points):
    #    A,B=get_bezier_coef(points)
    #    return [get_cubic(points[i],A[i],B[i],points[i+1]) for i in range(len(points)-1)]
