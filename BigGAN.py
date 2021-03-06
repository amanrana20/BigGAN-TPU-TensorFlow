import time

from tensorflow.contrib.opt import MovingAverageOptimizer

from ops import *
from utils import *

import logging
logger = logging.getLogger(__name__)

class BigGAN(object):

	def __init__(self, args):
		pass


	##################################################################################
	# Generator
	##################################################################################

	def generator(self, params, z, labels, is_training=True, reuse=False, getter=None):
		logger.debug("generator")
		cross_device = params['use_tpu']

		with tf.variable_scope("generator", reuse=reuse, custom_getter=getter):
			# 6
			if params['z_dim'] == 128:
				split_dim = 20
				split_dim_remainder = params['z_dim'] - (split_dim * 5)

				z_split = tf.split(z, num_or_size_splits=[split_dim] * 5 + [split_dim_remainder], axis=-1)

			else:
				split_dim = params['z_dim'] // 6
				split_dim_remainder = params['z_dim'] - (split_dim * 6)

				if split_dim_remainder == 0 :
					z_split = tf.split(z, num_or_size_splits=[split_dim] * 6, axis=-1)
				else :
					z_split = tf.split(z, num_or_size_splits=[split_dim] * 5 + [split_dim_remainder], axis=-1)

			ch = 16 * params['ch']
			sn = params['sn']

			x = fully_connected(z_split[0], units=4 * 4 * ch, sn=sn, scope='dense')
			x = tf.reshape(x, shape=[-1, 4, 4, ch])

			for i in range(params['layers']):
				x_size = x.shape[-2]

				if params['use_label_cond']:
					cond = tf.concat([z_split[i], labels], axis=-1)
				else:
					cond = z_split[i]

				x = resblock_up_condition(x, cond, channels=ch, use_bias=False, is_training=is_training, cross_device=cross_device, sn=sn, scope=f"resblock_up_w{x_size}_ch{ch//params['ch']}")
				
				x_size = x.shape[-2]
				if x_size in params['self_attn_res']:
					x = self_attention_2(x, channels=ch, sn=sn, scope=f"self_attention_w{x_size}_ch{ch//params['ch']}")

				ch = ch // 2

			ch = ch * 2

			x = batch_norm(x, is_training, cross_device=cross_device)
			x = relu(x)
			x = conv(x, channels=params['img_ch'], kernel=3, stride=1, pad=1, use_bias=False, sn=sn, scope='G_logit')
			x = tanh(x)

			# Crop down to expected size if spare pixels
			if x.shape[1] > params['img_size']:
				logger.warning(f"Cropping off {x.shape[1] - params['img_size']} pixels from width of generated images")
				x = x[:,:params['img_size'],:,:]
			
			if x.shape[2] > params['img_size']:
				logger.warning(f"Cropping off {x.shape[2] - params['img_size']} pixels from height of generated images")
				x = x[:,:,:params['img_size'],:]
			

			assert x.shape[1] == params['img_size'], "Generator architecture does not fit image size"
			assert x.shape[2] == params['img_size'], "Generator architecture does not fit image size"
			assert x.shape[3] == params['img_ch'],   "Generator architecture does not fit image channels"

			logger.debug("--")

			return x

	##################################################################################
	# Discriminator
	##################################################################################

	def discriminator(self, params, x, label, is_training=True, reuse=False):
		logger.debug("discriminator")
		with tf.variable_scope("discriminator", reuse=reuse):
			ch = params['ch']
			sn = params['sn']

			for i in range(params['layers']):

				x_size = x.shape[-2]
				x = resblock_down(x, channels=ch, use_bias=False, is_training=is_training, sn=sn, scope=f"resblock_down_w{x_size}_ch{ch//params['ch']}")

				x_size = x.shape[-2]
				if x_size in params['self_attn_res']:
					x = self_attention_2(x, channels=ch, sn=sn, scope=f"self_attention_w{x_size}_ch{ch//params['ch']}")
				
				ch = ch * 2

			ch = ch // 2

			x_size = x.shape[-2]
			x = resblock(x, channels=ch, use_bias=False, is_training=is_training, sn=sn, scope=f"resblock_w{x_size}_ch{ch//params['ch']}")
			x = relu(x)

			x = global_sum_pooling(x)

			label_embed = fully_connected(label, units=x.shape[-1], sn=sn, scope='D_label_embed')
			label_proj = x * label_embed

			x_scalar = fully_connected(x, units=1, sn=sn, scope='D_scalar')

			output = x_scalar + tf.reduce_sum(label_proj, axis=-1)

			logger.debug("--")

			return output

	def gradient_penalty(self, real, fake):
		if self.gan_type.__contains__('dragan'):
			eps = tf.random_uniform(shape=tf.shape(real), minval=0., maxval=1.)
			_, x_var = tf.nn.moments(real, axes=[0, 1, 2, 3])
			x_std = tf.sqrt(x_var)  # magnitude of noise decides the size of local region

			fake = real + 0.5 * x_std * eps

		alpha = tf.random_uniform(shape=[self.batch_size, 1, 1, 1], minval=0., maxval=1.)
		interpolated = real + alpha * (fake - real)

		logit = self.discriminator(interpolated, reuse=True)

		grad = tf.gradients(logit, interpolated)[0]  # gradient of D(interpolated)
		grad_norm = tf.norm(flatten(grad), axis=1)  # l2 norm

		GP = 0

		# WGAN - LP
		if self.gan_type == 'wgan-lp':
			GP = self.ld * tf.reduce_mean(tf.square(tf.maximum(0.0, grad_norm - 1.)))

		elif self.gan_type == 'wgan-gp' or self.gan_type == 'dragan':
			GP = self.ld * tf.reduce_mean(tf.square(grad_norm - 1.))

		return GP

	##################################################################################
	# Model
	##################################################################################

	def base_model_fn(self, features, labels, mode, params):
		'''
		
		All the model function heavy lifting is done here, agnostic of whether
		it'll be used in an Estimator or TPUEstimator

		'''

		params = EasyDict(**params)

		# --------------------------------------------------------------------------
		# Core GAN model
		# --------------------------------------------------------------------------
		

		# Because we cannot pass in labels in predict mode (despite them being useful 
		# for GANs), I've passed the labels in as the (otherwise unneeded) features
		# it's a bit of a hack, sorry.
		if mode == tf.estimator.ModeKeys.PREDICT:
			labels = features
		

		# Latent input to generate images
		if mode == tf.estimator.ModeKeys.TRAIN:
			z = tf.random.normal(shape=[params.batch_size, params.z_dim], name='random_z')
		else:
			# The "truncated normal" trick to make generated predictions nicer looking
			z = tf.random.truncated_normal(shape=[params.batch_size, params.z_dim], name='random_z')
		
		# generate and critique fake images
		fake_images = self.generator(params, z, labels)
		fake_logits = self.discriminator(params, fake_images, labels)
		g_loss = generator_loss(params.gan_type, fake=fake_logits)

		# Train the discriminator
		if mode in [tf.estimator.ModeKeys.TRAIN, tf.estimator.ModeKeys.EVAL]:
			real_logits = self.discriminator(params, features, labels, reuse=True)

			if params.gan_type.__contains__('wgan') or params.gan_type == 'dragan':
				GP = self.gradient_penalty(real=features, fake=fake_images)
			else:
				GP = 0

			d_loss = discriminator_loss(params.gan_type, real=real_logits, fake=fake_logits) + GP

		else:
			d_loss = 0


		# --------------------------------------------------------------------------
		# Vars for training and evaluation
		# --------------------------------------------------------------------------
		
		t_vars = tf.trainable_variables()
		d_vars = [var for var in t_vars if 'discriminator' in var.name]
		g_vars = [var for var in t_vars if 'generator' in var.name]

		# --------------------------------------------------------------------------
		# Averaging var values can help with eval/prediction
		# http://ruishu.io/2017/11/22/ema/
		# --------------------------------------------------------------------------
		
		ema = tf.train.ExponentialMovingAverage(decay=params['moving_decay'])

		def ema_getter(getter, name, *args, **kwargs):
			var = getter(name, *args, **kwargs)
			ema_var = ema.average(var)
			return ema_var if ema_var is not None else var

		# --------------------------------------------------------------------------
		# Loss
		# --------------------------------------------------------------------------

		if mode != tf.estimator.ModeKeys.PREDICT:
			loss = g_loss
			for i in range(params.n_critic):
				loss += d_loss
		else:
			loss = 0


		# --------------------------------------------------------------------------
		# Training op
		# --------------------------------------------------------------------------

		if mode == tf.estimator.ModeKeys.TRAIN:
			# Create training ops for both D and G

			d_optimizer = tf.train.AdamOptimizer(params.d_lr, beta1=params.beta1, beta2=params.beta2)
			
			if params.use_tpu:
				d_optimizer = tf.contrib.tpu.CrossShardOptimizer(d_optimizer)

			d_train_op = d_optimizer.minimize(d_loss, var_list=d_vars, global_step=tf.train.get_global_step())

			
			g_optimizer = tf.train.AdamOptimizer(params.g_lr, beta1=params.beta1, beta2=params.beta2)
			
			if params.use_tpu:
				g_optimizer = tf.contrib.tpu.CrossShardOptimizer(g_optimizer)

			g_train_op = g_optimizer.minimize(g_loss, var_list=g_vars, global_step=tf.train.get_global_step())


			# For each training op of G, do n_critic training ops of D
			train_ops = [g_train_op]
			for i in range(params.n_critic):
				train_ops.append(d_train_op)
			train_op = tf.group(*train_ops)

			with tf.control_dependencies([train_op]):
				# Create the shadow variables, and add ops to maintain moving averages
				# of var0 and var1. This also creates an op that will update the moving
				# averages after each training step.  This is what we will use in place
				# of the usual training op.
				train_op = ema.apply(g_vars)

		else:
			train_op = None

		# --------------------------------------------------------------------------
		# Predictions
		# --------------------------------------------------------------------------

		predict_fake_images = self.generator(params, z, labels, reuse=True, getter=ema_getter)

		predictions = {
			"fake_image": predict_fake_images,
			"labels": labels,
		}

		# --------------------------------------------------------------------------
		# Eval metrics
		# --------------------------------------------------------------------------
		
		if mode == tf.estimator.ModeKeys.EVAL:

			# Hack to allow it out of a fixed batch size TPU
			d_loss_batched = tf.tile(tf.expand_dims(d_loss, 0), [params.batch_size])
			g_loss_batched = tf.tile(tf.expand_dims(g_loss, 0), [params.batch_size])

			d_grad = tf.gradients(d_loss, d_vars)
			g_grad = tf.gradients(g_loss, g_vars)

			d_grad_joined = tf.concat([
				tf.reshape(i, [-1]) for i in d_grad
			], axis=-1)

			g_grad_joined = tf.concat([
				tf.reshape(i, [-1]) for i in g_grad
			], axis=-1)

			def metric_fn(d_loss, g_loss, fake_logits, d_grad, g_grad):
				return {
					"d_loss"      : tf.metrics.mean(d_loss),
					"g_loss"      : tf.metrics.mean(g_loss),
					"fake_logits" : tf.metrics.mean(fake_logits),
					"d_grad"      : tf.metrics.mean(d_grad),
					"g_grad"      : tf.metrics.mean(g_grad),
				}

			metric_fn_args = [d_loss_batched, g_loss_batched, fake_logits, d_grad_joined, g_grad_joined]

		else:
			metric_fn = None
			metric_fn_args = None

		# --------------------------------------------------------------------------
		# Alright, all built!
		# --------------------------------------------------------------------------

		return loss, train_op, predictions, metric_fn, metric_fn_args

	def gpu_model_fn(self, features, labels, mode, params):

		loss, train_op, predictions, metric_fn, metric_fn_args = self.base_model_fn(features, labels, mode, params)

		if mode == tf.estimator.ModeKeys.PREDICT:
			return tf.estimator.EstimatorSpec(mode, predictions=predictions)

		if mode == tf.estimator.ModeKeys.EVAL:
			return tf.estimator.EstimatorSpec(
				mode=mode,
				loss=loss, 
				eval_metric_ops=metric_fn(*metric_fn_args)
			)

		if mode == tf.estimator.ModeKeys.TRAIN:
			return tf.estimator.EstimatorSpec(mode, loss=loss, train_op=train_op)


	def tpu_model_fn(self, features, labels, mode, params):

		loss, train_op, predictions, metric_fn, metric_fn_args = self.base_model_fn(features, labels, mode, params)

		if mode == tf.estimator.ModeKeys.PREDICT:
		    return tf.contrib.tpu.TPUEstimatorSpec(mode, predictions=predictions)
	
		if mode == tf.estimator.ModeKeys.EVAL:
			return tf.contrib.tpu.TPUEstimatorSpec(
				mode=mode,
				loss=loss, 
				eval_metrics=(
					metric_fn, 
					metric_fn_args
				)
			)

		if mode == tf.estimator.ModeKeys.TRAIN:
			return tf.contrib.tpu.TPUEstimatorSpec(mode, loss=loss, train_op=train_op)


