import adapter from '@sveltejs/adapter-node';

/** @type {import('@sveltejs/kit').Config} */
const config = {
	kit: {
		// Using adapter-node as requested for specific environment deployment
		adapter: adapter()
	}
};

export default config;
