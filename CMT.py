import ipdb
import cv2
import gnebehay
import itertools
import numpy as np
import scipy.cluster
import scipy.spatial
import time

from numpy import *

class CMT(object):

	DETECTOR = 'BRISK'
	DESCRIPTOR = 'BRISK'
	DESC_LENGTH = 512
	MATCHER = 'BruteForce-Hamming'
	THR_OUTLIER = 20 #Completely arbritary
	THR_CONF = 0.75 #Completely arbritary
	THR_RATIO = 0.8 #Because Lowe said so

	estimate_scale = True
	estimate_rotation = True

	def initialise(self, im_gray0, tl, br):

		#Initialise detector, descriptor, matcher
		self.detector = cv2.FeatureDetector_create(self.DETECTOR)
		self.descriptor = cv2.DescriptorExtractor_create(self.DESCRIPTOR)
		self.matcher = cv2.DescriptorMatcher_create(self.MATCHER)

		#Get initial keypoints in whole image
		keypoints_cv = self.detector.detect(im_gray0)

		#Remember keypoints that are in the rectangle as selected keypoints
		ind = gnebehay.in_rect(keypoints_cv, tl, br)
		selected_keypoints_cv = list(itertools.compress(keypoints_cv, ind))
		selected_keypoints_cv, self.selected_features = self.descriptor.compute(im_gray0, selected_keypoints_cv)
		selected_keypoints = gnebehay.keypoints_cv_to_np(selected_keypoints_cv)
		num_selected_keypoints = len(selected_keypoints_cv)

		if num_selected_keypoints == 0:
			raise Exception('No keypoints found in selection')

		#Remember keypoints that are not in the rectangle as background keypoints
		background_keypoints_cv = list(itertools.compress(keypoints_cv, ~ind))
		background_keypoints_cv, background_features = self.descriptor.compute(im_gray0, background_keypoints_cv)
		background_keypoints = gnebehay.keypoints_cv_to_np(background_keypoints_cv)

		#Assign each keypoint a class starting from 1, background is 0
		self.selected_classes = array(range(num_selected_keypoints)) + 1
		background_classes = zeros(len(background_keypoints_cv))

		#Stack background features and selected features into database
		self.features_database = vstack((background_features, self.selected_features))

		#Same for classes
		self.database_classes = hstack((background_classes, self.selected_classes))

		#Get all distances between selected keypoints in squareform
		pdist = scipy.spatial.distance.pdist(selected_keypoints)
		self.squareform = scipy.spatial.distance.squareform(pdist)

		#Get all angles between selected keypoints
		angles = np.empty((num_selected_keypoints, num_selected_keypoints))
		for k1,i1 in zip(selected_keypoints, range(num_selected_keypoints)):
			for k2,i2 in zip(selected_keypoints, range(num_selected_keypoints)):

				#Compute vector from k1 to k2
				v = k2-k1

				#Compute angle of this vector with respect to x axis
				angle = math.atan2(v[1],v[0])

				#Store angle
				angles[i1,i2] = angle

		self.angles = angles

		#Find the center of selected keypoints
		center = np.mean(selected_keypoints, axis=0)

		#Remember the rectangle coordinates relative to the center
		self.center_to_tl = np.array(tl) - center
		self.center_to_tr = np.array([br[0],tl[1]]) - center
		self.center_to_br = np.array(br) - center
		self.center_to_bl = np.array([tl[0],br[1]]) - center

		#Calculate springs of each keypoint
		self.springs = selected_keypoints - center

		#Set start image for tracking
		self.im_prev = im_gray0

		#Make keypoints 'active' keypoints
		self.active_keypoints = np.copy(selected_keypoints)

		#Attach class information to active keypoints
		self.active_keypoints = hstack((selected_keypoints,self.selected_classes[:,None]))

		#Remember number of initial keypoints
		self.num_initial_keypoints = len(selected_keypoints_cv)

	def estimate(self, keypoints):

		tic = time.time()

		center = array((nan,nan))
		scale_estimate = nan
		med_rot = nan

		#At least 2 keypoints are needed for scale
		if keypoints.size > 1:

			#Extract the keypoint classes
			#Careful: tracked_classes is assumed to be sorted - take care of it at end of loop
			#AND it might contain duplicate elements
			keypoint_classes = keypoints[:,2].squeeze().astype(np.int) 

			#Retain singular dimension
			if keypoint_classes.size == 1:
				keypoint_classes = keypoint_classes[None]

			#Sort
			#TODO: Why do we sort here if we assume it is already sorted?
			ind_sort = argsort(keypoint_classes)
			keypoints = keypoints[ind_sort]
			keypoint_classes = keypoint_classes[ind_sort]

			#Get all combinations of keypoints
			all_combs = array([val for val in itertools.product(range(keypoints.shape[0]),repeat = 2)])	

			#But exclude comparison with itself
			all_combs = all_combs[all_combs[:,0] != all_combs[:,1],:]

			#Measure distance between allcombs[0] and allcombs[1]
			#TODO: Wrong comment?
			ind1 = all_combs[:,0] 
			ind2 = all_combs[:,1]

			class_ind1 = keypoint_classes[ind1] - 1
			class_ind2 = keypoint_classes[ind2] - 1

			duplicate_classes = class_ind1 == class_ind2

			if not all(duplicate_classes):
				ind1 = ind1[~duplicate_classes]
				ind2 = ind2[~duplicate_classes]

				class_ind1 = class_ind1[~duplicate_classes]
				class_ind2 = class_ind2[~duplicate_classes]

				pts_allcombs0 = keypoints[ind1,:2]
				pts_allcombs1 = keypoints[ind2,:2]

				#This distance might be 0 for some combinations,
				#as it can happen that there is more than one keypoint at a single location
				dists = gnebehay.L2norm(pts_allcombs0 - pts_allcombs1)

				original_dists = self.squareform[class_ind1,class_ind2]

				scalechange = dists / original_dists

				#Compute angles

				angles = np.empty((pts_allcombs0.shape[0]))


				#We will now try to vectorise this loop
				v=pts_allcombs1 - pts_allcombs0
				angles = np.arctan2(v[:,1],v[:,0])
				
				original_angles = self.angles[class_ind1,class_ind2]

				angle_diffs = angles - original_angles

				#Fix long way angles
				long_way_angles = np.abs(angle_diffs) > math.pi

				angle_diffs[long_way_angles] = angle_diffs[long_way_angles] - np.sign(angle_diffs[long_way_angles]) * 2 * math.pi


				scale_estimate = median(scalechange)
				if not self.estimate_scale:
					scale_estimate = 1;

				med_rot = median(angle_diffs)
				if not self.estimate_rotation:
					med_rot = 0;

				keypoint_class = keypoints[:,2].astype(np.int)
				votes = keypoints[:,:2] - scale_estimate * (gnebehay.rotate(self.springs[keypoint_class-1], med_rot))

				#Remember all votes including outliers
				self.votes = votes

				#Compute pairwise distance between votes
				pdist = scipy.spatial.distance.pdist(votes)

				#Compute linkage between pairwise distances
				linkage = scipy.cluster.hierarchy.linkage(pdist)

				#Perform hierarchical distance-based clustering
				T = scipy.cluster.hierarchy.fcluster(linkage, self.THR_OUTLIER, criterion='distance')

				#Count votes for each cluster
				cnt = np.bincount(T) #Dummy 0 label remains
				
				#Get largest class
				Cmax = argmax(cnt)

				#Identify inliers (=members of largest class)
				inliers = T == Cmax
				#inliers = med_dists < THR_OUTLIER

				#Remember outliers
				self.outliers = keypoints[~inliers,:]

				#Stop tracking outliers
				keypoints = keypoints[inliers,:]

				#Remove outlier votes
				votes = votes[inliers,:]

				#Compute object center
				center = np.mean(votes, axis=0)

				#TODO: Shouldn't we recompute scale_estimate, med_rot here?

		return (center, scale_estimate, med_rot, keypoints)

	#Detection, Description and Tracking of keypoints is quite fast.
	#Most of the time is spent in estimate
	def process_frame(self, im_gray):
		#Track keypoints
		tic = time.time()
		tracked_keypoints, status = gnebehay.track(self.im_prev, im_gray, self.active_keypoints)
		toc = time.time()
		#print ' track_time: {0:.0f}ms'.format(1000*(toc-tic))
		tic = time.time()
		(center, scale_estimate, rotation_estimate, tracked_keypoints) = self.estimate(tracked_keypoints)
		toc = time.time()
		#print ' estimate_time: {0:.0f}ms'.format(1000*(toc-tic))

		#Detect keypoints, compute descriptors
		tic = time.time()
		keypoints_cv = self.detector.detect(im_gray) 
		keypoints_cv, features = self.descriptor.compute(im_gray, keypoints_cv)
		toc = time.time()
		#print ' det_desc_time: {0:.0f}ms'.format(1000*(toc-tic))

		tic = time.time()

		#Create list of active keypoints
		active_keypoints = zeros((0,3)) 

		#For each keypoint and its descriptor
		#TODO: This loop could probably be optimised, as some comparisons are unnecessary
		if len(keypoints_cv) > 0:
			for (keypoint_cv, feature) in zip(keypoints_cv, features):

				#Retrieve keypoint location
				location = np.array(keypoint_cv.pt)

				#First: Match over whole image
				#Compute distances to all descriptors
				#TODO: Why do we match to feature database here, but to selected features above?
				matches = self.matcher.match(self.features_database, feature[None,:])
				distances = np.array([m.distance for m in matches])

				#Convert distances to confidences, do not weight
				combined = 1 - distances / self.DESC_LENGTH

				classes = self.database_classes

				#Sort in descending order
				sorted_conf = argsort(combined)[::-1] #reverse

				#Get best and second best index
				bestInd = sorted_conf[0]
				secondBestInd = sorted_conf[1]

				#Compute distance ratio according to Lowe
				ratio = (1-combined[bestInd]) / (1-combined[secondBestInd]) #TODO: Use combined values or distances?

				#Extract class of best match
				keypoint_class = classes[bestInd]

				#If distance ratio is ok and absolute distance is ok and keypoint class is not background
				if ratio < self.THR_RATIO and combined[bestInd] > self.THR_CONF and keypoint_class != 0:

					#Add keypoint to active keypoints
					new_kpt = append(location, keypoint_class)
					active_keypoints = append(active_keypoints, array([new_kpt]), axis=0)

				#In a second step, try to match difficult keypoints
				#If structural constraints are applicable
				if not any(isnan(center)):

					#Compute distances to initial descriptors
					matches = self.matcher.match(self.selected_features, feature[None,:])
					distances = np.array([m.distance for m in matches])

					#Convert distances to confidences
					confidences = 1 - distances / self.DESC_LENGTH

					#Compute the keypoint location relative to the object center
					relative_location = location - center

					#Compute the distances to all springs
					displacements = gnebehay.L2norm(scale_estimate * gnebehay.rotate(self.springs, -rotation_estimate) - relative_location)

					#For each spring, calculate weight
					weight = displacements < self.THR_OUTLIER #Could be smooth function

					combined = weight * confidences

					classes = self.selected_classes

					#Sort in descending order
					sorted_conf = argsort(combined)[::-1] #reverse

					#Get best and second best index
					bestInd = sorted_conf[0]
					secondBestInd = sorted_conf[1]

					#Compute distance ratio according to Lowe
					ratio = (1-combined[bestInd]) / (1-combined[secondBestInd]) #TODO: Use combined values or distances?

					#Extract class of best match
					keypoint_class = classes[bestInd]

					#If distance ratio is ok and absolute distance is ok and keypoint class is not background
					if ratio < self.THR_RATIO and combined[bestInd] > self.THR_CONF and keypoint_class != 0:

					#Add keypoint to active keypoints
						new_kpt = append(location, keypoint_class)

						#Check whether same class already exists
						if active_keypoints.size > 0:
							same_class = np.nonzero(active_keypoints[:,2] == keypoint_class)
							active_keypoints = np.delete(active_keypoints, same_class, axis=0)

						active_keypoints = append(active_keypoints, array([new_kpt]), axis=0)



		#If some keypoints have been tracked
		if tracked_keypoints.size > 0:

			#Extract the keypoint classes
			tracked_classes = tracked_keypoints[:,2]

			#If there already are some active keypoints
			if active_keypoints.size > 0:

				#Add all tracked keypoints that have not been matched
				associated_classes = active_keypoints[:,2]
				missing = ~np.in1d(tracked_classes, associated_classes)
				active_keypoints = append(active_keypoints, tracked_keypoints[missing,:], axis=0)

			#Else use all tracked keypoints
			else:
				active_keypoints = tracked_keypoints

		#Update object state estimate
		active_keypoints_before = active_keypoints
		toc = time.time()
		#print ' some_time: {0:.0f}ms'.format(1000*(toc-tic))
		tic = time.time()
		#Don't do it again
		#(center, scale_estimate, rotation_estimate, active_keypoints) = self.estimate(active_keypoints)
		self.center = center
		self.scale_estimate = scale_estimate
		self.rotation_estimate = rotation_estimate
		self.tracked_keypoints = tracked_keypoints
		self.active_keypoints = active_keypoints
		self.im_prev = im_gray
		self.keypoints_cv = keypoints_cv
		toc = time.time()
		#print ' estimate_time: {0:.0f}ms'.format(1000*(toc-tic))

		self.tl = (nan,nan)
		self.tr = (nan,nan)
		self.br = (nan,nan)
		self.bl = (nan,nan)

		self.bb = array([nan,nan,nan,nan])

		self.has_result = False
		if not any(isnan(self.center)) and self.active_keypoints.shape[0] > self.num_initial_keypoints / 10:
			self.has_result = True

			tl = gnebehay.array_to_int_tuple(center + scale_estimate*gnebehay.rotate(self.center_to_tl[None,:], rotation_estimate).squeeze())
			tr = gnebehay.array_to_int_tuple(center + scale_estimate*gnebehay.rotate(self.center_to_tr[None,:], rotation_estimate).squeeze())
			br = gnebehay.array_to_int_tuple(center + scale_estimate*gnebehay.rotate(self.center_to_br[None,:], rotation_estimate).squeeze())
			bl = gnebehay.array_to_int_tuple(center + scale_estimate*gnebehay.rotate(self.center_to_bl[None,:], rotation_estimate).squeeze())

			min_x = min((tl[0],tr[0],br[0],bl[0]))
			min_y = min((tl[1],tr[1],br[1],bl[1]))
			max_x = max((tl[0],tr[0],br[0],bl[0]))
			max_y = max((tl[1],tr[1],br[1],bl[1]))

			self.tl = tl
			self.tr = tr
			self.bl = bl
			self.br = br

			self.bb = np.array([min_x, min_y, max_x - min_x, max_y - min_y])

