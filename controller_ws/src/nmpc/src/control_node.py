#!/usr/bin/env python
import rospy
from std_msgs.msg import Float64MultiArray
from std_msgs.msg import Float32
from geometry_msgs.msg import PoseStamped
from nav_msgs.msg import OccupancyGrid
from nav_msgs.msg import Path
from std_msgs.msg import String
from std_msgs.msg import Bool
from nav_msgs.msg import Path
from std_msgs.msg import UInt8
from std_msgs.msg import Float64
from geometry_msgs.msg import Point
from geometry_msgs.msg import Pose2D
from sensor_msgs.msg import Imu
from gazebo_msgs.msg import ModelStates
from io import StringIO
import numpy as np
import sys
import os
import do_mpc
from do_mpc.data import save_results, load_results
from casadi import *
import matplotlib.pyplot as plt
import matplotlib as mpl
import pickle
sys.path.append('../../')
    


class MPController():
    def __init__(self, N_ref = 1):
        
        rospy.init_node('control_node')
        #Model and MPC variables
        self.model_type = 'continuous'  
        self.J = 375            # moment of interia
        self.La = 1.2           # distance of front tires from COM in m
        self.Lb = 1.2           # distance of back tires from COM in m
        self.m = 200            # mass of vehicle in kg
        self.Cy = 0.1           # Tyre stiffness constant
        self.t_s = 0.1          # sample time
        self.N = 1              # Horizon
        self.N_ref = N_ref      # control iterations
    
        # variables for control execution
        self.velocities = [1.5]    # array to store velocities
        self.path_points = []      # array to store the path
        self.c = 1
        self.packed = [self.velocities, self.c]
        self.x_initial = vertcat([0],[0],[0],[1.5],[0],[0],[0])       # intial state vector
        self.x_0 = self.x_initial 
        self.steer = 0          # variable to store the curent state vector
        self.z_sim = 0

        self.conversion_matrix_x = np.zeros((8,1))   
        self.conversion_matrix_y = np.zeros((8,1))
        self.conversion_matrix_v = np.zeros((8,1))

        #MPC  variables
        self.model = self.mpc_model()
        self.controller = self.controller(self.model)
        self.simulator = self.mpc_simulator(self.model)
        self.estimator = do_mpc.estimator.StateFeedback(self.model)
        
        # ROS variables
        
        self.rate = rospy.Rate(100)
        self.path_sub = rospy.Subscriber("/path", Path, self.path_callback)
        self.vel_sub = rospy.Subscriber("/velocity_array2", Float64MultiArray,self.vel_callback)
        self.state_sub = rospy.Subscriber("/gazebo/model_states", ModelStates, self.state_callback)
        self.steer_sub = rospy.Subscriber("/current_steer_angle", Float64, self.steer_callback)
        self.control_sub = rospy.Subscriber("/path", Path, self.control_callback)

        self.acc_pub = rospy.Publisher('/throttle_cmd', Float64, queue_size=10)
        self.brake_pub = rospy.Publisher('/brake_cmd', Float64, queue_size=10)
        self.steer_pub = rospy.Publisher('/steering_cmd', Float64, queue_size=10)
        self.gear_pub = rospy.Publisher('/gear_cmd', UInt8, queue_size=10)

        self.simulator.x0 = self.x_initial
        self.controller.x0 = self.x_initial
        self.estimator.x0 = self.x_initial
        self.controller.set_initial_guess()
        self.simulator.set_initial_guess()
        self.controller.reset_history()
        self.simulator.reset_history()
        rospy.spin()

    def linear_regression(self, x, n=0, lamda=0):
        n = len(x)
        z0 = np.array([1 for i in range(n)])
        z1 = np.array([i+1 for i in range(n)])
        z2 = z1**2
        z3 = z1**3
        z4 = z1**4
        z5 = z1**5
        z6 = z1**6
        z7 = z1**7
        z = np.vstack((z0, z1, z2, z3, z4, z5, z6, z7)).T
        return np.dot(np.linalg.pinv(np.dot(z.T,z)),(np.dot(z.T,x)))

    def lin_val_x(self, z):
        self.conversion_matrix_x = self.linear_regression(z)

    def lin_val_y(self, z):
        self.conversion_matrix_y = self.linear_regression(z)

    def lin_val_v(self, z):
        self.conversion_matrix_v = self.linear_regression(z)

    def x_z(self,z, x_c0):
        c = self.conversion_matrix_x
        x_c0['x_c0', 0] = c[0]
        a = c[0]*1 + c[1]*z + c[2]*(z**2) + c[3]*(z**3) + c[4]*(z**4) + c[5]*(z**5) + c[6]*(z**6) + c[7]*(z**7)
        return a

    def y_z(self,z):
        c = self.conversion_matrix_y
        a = c[0]*1 + c[1]*z + c[2]*(z**2) + c[3]*(z**3) + c[4]*(z**4) + c[5]*(z**5) + c[6]*(z**6) + c[7]*(z**7) 
        return a

    def v_z(self,z):
        c = self.conversion_matrix_v
        a = c[0]*1 + c[1]*z + c[2]*(z**2) + c[3]*(z**3) + c[4]*(z**4) + c[5]*(z**5) + c[6]*(z**6) + c[7]*(z**7) 
        return a


    def mpc_model(self):
        # Obtain an instance of the do-mpc model class
        model = do_mpc.model.Model(self.model_type)

        # States variables of the model
        xc = model.set_variable(var_type='_x',var_name='xc',shape = (1,1))                     # x position
        yc = model.set_variable(var_type='_x',var_name='yc',shape = (1,1))                     # y position
        v = model.set_variable(var_type='_x',var_name='v',shape = (1,1))                       # velocity
        theta = model.set_variable(var_type='_x',var_name='theta',shape = (1,1))               # yaw angle
        phi = model.set_variable(var_type='_x',var_name='phi',shape = (1,1))                   # yaw angular velocity
        delta = model.set_variable(var_type='_x',var_name='delta',shape = (1,1))               # steering angle

        x_c0 = model.set_variable(var_type='_p',var_name='x_c0',shape = (1,1))
        x_c1 = model.set_variable(var_type='_p',var_name='x_c1',shape = (1,1))
        x_c2 = model.set_variable(var_type='_p',var_name='x_c2',shape = (1,1))
        x_c3 = model.set_variable(var_type='_p',var_name='x_c3',shape = (1,1))
        x_c4 = model.set_variable(var_type='_p',var_name='x_c4',shape = (1,1))
        x_c5 = model.set_variable(var_type='_p',var_name='x_c5',shape = (1,1))
        x_c6 = model.set_variable(var_type='_p',var_name='x_c6',shape = (1,1))
        x_c7 = model.set_variable(var_type='_p',var_name='x_c7',shape = (1,1))

        y_c0 = model.set_variable(var_type='_p',var_name='y_c0',shape = (1,1))
        y_c1 = model.set_variable(var_type='_p',var_name='y_c1',shape = (1,1))
        y_c2 = model.set_variable(var_type='_p',var_name='y_c2',shape = (1,1))
        y_c3 = model.set_variable(var_type='_p',var_name='y_c3',shape = (1,1))
        y_c4 = model.set_variable(var_type='_p',var_name='y_c4',shape = (1,1))
        y_c5 = model.set_variable(var_type='_p',var_name='y_c5',shape = (1,1))
        y_c6 = model.set_variable(var_type='_p',var_name='y_c6',shape = (1,1))
        y_c7 = model.set_variable(var_type='_p',var_name='y_c7',shape = (1,1))

        v_c0 = model.set_variable(var_type='_p',var_name='v_c0',shape = (1,1))
        v_c1 = model.set_variable(var_type='_p',var_name='v_c1',shape = (1,1))
        v_c2 = model.set_variable(var_type='_p',var_name='v_c2',shape = (1,1))
        v_c3 = model.set_variable(var_type='_p',var_name='v_c3',shape = (1,1))
        v_c4 = model.set_variable(var_type='_p',var_name='v_c4',shape = (1,1))
        v_c5 = model.set_variable(var_type='_p',var_name='v_c5',shape = (1,1))
        v_c6 = model.set_variable(var_type='_p',var_name='v_c6',shape = (1,1))
        v_c7 = model.set_variable(var_type='_p',var_name='v_c7',shape = (1,1))


        # virtual state for timing law
        z = model.set_variable(var_type='_x', var_name='z', shape = (1,1))
    
        #control inputs
        a = model.set_variable(var_type='_u',var_name='a',shape = (1,1))    # acceleration
        w = model.set_variable(var_type='_u',var_name='w',shape = (1,1))    # steering rate (angular)

        # virtual control input for timing law
        u_v = model.set_variable(var_type='_u',var_name='u_v', shape=(1,1))

        # Set right-hand-side of ODE for all introduced states (_x).
        # Names are inherited from the state definition.
        
        Fyf = self.Cy * (delta - (self.La * phi) / (v+1e-8))
        Fyr = (self.Cy * self.Lb * phi) / (v+1e-8)
        
        equations = vertcat( v * np.sin(theta), 
                           v * np.cos(theta),
                           a * np.cos(delta) - (2.0 / self.m) * Fyf * np.sin(delta),
                           phi,
                           (1.0 / self.J) * (self.La * (self.m * a * np.sin(delta) + 2 * Fyf * np.cos(delta)) - 2 * self.Lb * Fyr),
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
        v = model.x['v']
        z = model.x['z']
        x_c0 = model.p
            
        # Objective function:

        # cost = (xc-1) ** 2 + (yc-10) ** 2 + (model.x['v']-1) ** 2
        cost = (xc - x_c0['x_c0',0]*z) ** 2 + (yc - self.y_z(z)) ** 2 + (v - self.v_z(z)) ** 2
        mterm = cost # terminal cost
        lterm = cost # stage cost

        mpc.set_objective(mterm=mterm, lterm=lterm)
        mpc.set_rterm(a=0.00005)
        mpc.set_rterm(w=0.00001) # Scaling for quad. cost.

        ####################### State and input bounds #######################3

        # mpc.bounds['lower','_x','xc'] = x_0[0]-1e-4
        # mpc.bounds['lower','_x','yc'] = y_lower
        mpc.bounds['lower','_x','v'] = 0 #max reverse speed in m/s
        mpc.bounds['lower','_x','theta'] = -50
        mpc.bounds['lower','_x','phi'] = -50
        mpc.bounds['lower','_x','delta'] = -np.pi/6
        # mpc.bounds['upper','_x','xc'] = target_x+0.1
        # mpc.bounds['upper','_x','yc'] = y_upper
        mpc.bounds['upper','_x','v'] = 2.2 #max forward speed in m/s
        mpc.bounds['upper','_x','theta'] = 50
        mpc.bounds['upper','_x','phi'] = 50
        mpc.bounds['upper','_x','delta'] = np.pi/6



        mpc.bounds['lower','_u','a'] = -10
        mpc.bounds['lower','_u','w'] = -10
        mpc.bounds['upper','_u','a'] = 10
        mpc.bounds['upper','_u','w'] = 10

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

    def control_callback(self, control):

        ###############################################################################################
        #Set the initial conditions for first iteration
        # self.simulator.x0 = self.x_initial  
        # x_0 = self.simulator.x0.cat.full()
        # self.controller.x0 = x_0
        # self.estimator.x0 = x_0

        # self.controller.set_initial_guess()
        # self.simulator.set_initial_guess()
        # self.controller.reset_history()
        # self.simulator.reset_history()
        ###############################################################################################

        # Defining arrays for state, path and velocity

        a = 2
        
    def path_callback(self, path):

        path_repeat = False
        
        ##################################
        # Step1 - Obtaining the path points
        ##################################
        
        l = len(path.poses)                                                   # length of the path
        points = np.zeros((l,2))                                              # array for storing the path points
        # check for repetition on the path. 
        # if len(self.path_points) > 0:                                            # Prevents check the first time this fucntion is called
        #     if self.path_points[0][0] == path.poses[0].pose.position.y:            # checks the y-coordinate of path
        #         print("### repetition detected ###")
        #         path_repeat = True
        #         self.path_sub.unregister()                                       # unsubscribe the topic
        #         print("####################################### path unregistered #######################################")

        # after check passed
        if not path_repeat:    
            for j in range(l):
                points[j][0] = path.poses[j].pose.position.y
                points[j][1] = path.poses[j].pose.position.x
                self.path_points.append([path.poses[j].pose.position.y, path.poses[j].pose.position.x])


        ###########################
        # Step2 - Interplotate
        ###########################

        self.z_sim = 0
        self.lin_val_x(points[:,1])
        self.lin_val_y(points[:,0])


        state = []
        x = []
        y = []
        v = []             
        # self.steer = self.simulator.x0['delta']  
        # Start the control loop
        print(self.conversion_matrix_x, self.conversion_matrix_y)
        print("BEFORE STATE:", self.x_0)

        u0 = self.controller.make_step(self.x_0)                  # Determine optimal control inputs using the inital state given

        # publish the steering angle and acceleration and brake values 
        #self.acc_pub.publish(abs(u0[0][0]))
        if u0[0][0]>=0:
            self.acc_pub.publish(u0[0][0])
        else:
            force = u0[0][0] * self.m
            torque = -0.32 * force
            self.brake_pub.publish(torque)

        self.steer += u0[1][0]
        self.steer_pub.publish(-self.steer*17)
        self.gear_pub.publish(0)

        print("CONTROL INPUTS:", u0)
        y_n = self.simulator.make_step(u0)                  # Simulate the next step using the control inputs
        print("AFTER STATE:", y_n)
        # self.x_0 = self.estimator.make_step(y_n)            # Estimate the next state
             
        self.z_sim = y_n[6]


    def vel_callback(self, vel_arr):

        self.velocities.append(vel_arr.data[0])

        ######################################
        # Step1 - Obtaining the velocity points
        ######################################

        l = len(vel_arr.data)
        vel = np.zeros((l, 1))
        for i in range(l):
            vel[i][0] = vel_arr.data[i] 

        ###########################
        # Step2 - Interplotate
        ###########################

        # interpolation using the indices as the variable.
        self.lin_val_v(vel)



    def state_callback(self, state):
        # get the state 
        # update the class variable for curent state value
        
        ######################################
        # Step1 - Obtaining the state vector
        ######################################

        #l_pose = len(state.pose[-1])
        #l_twist = len(state.twist[-1])
        pose_arr = []
        twist_arr = [] 
        car_x = state.pose[11].position.x
        car_y = state.pose[11].position.y

        pose_arr.append(state.pose[-1].position.x)
        pose_arr.append(state.pose[-1].position.y)

        # pose_arr[2] = state.pose.orientation.x
        # pose_arr[3] = state.pose.orientation.y
        # pose_arr[4] = state.pose.orientation.z
        # pose_arr[5] = state.pose.orientation.w

        twist_arr.append(state.twist[-1].linear.x)
        twist_arr.append(state.twist[-1].linear.y)
        twist_arr.append(state.twist[-1].angular.z)

        ####################################
        # Step2 - Update the function handles
        ####################################

        self.x_0 = vertcat(   [pose_arr[0]],
                              [pose_arr[1]],
                              [float(np.sqrt(twist_arr[0]**2 + twist_arr[1]**2))],
                              [float(np.arctan(twist_arr[1]/(twist_arr[0]+(1e-50))))],
                              [float(twist_arr[2])],
                              [self.steer],
                              [self.z_sim]
                        )


    def steer_callback(self, steer):
        # get the state 
        # update the class variable for curent state value

        ####################################
        # Step1 - Update the function handles
        ####################################

        self.steer = steer.data



if __name__  == '__main__':

    # Create class object 
    controlObj = MPController()
    # run the control loop
    
    













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
